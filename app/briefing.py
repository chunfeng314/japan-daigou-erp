"""每日简报 —— 「哪里卡住了」一眼看到。

对应开发文档 §9「卡单检测 + 每日待办（**规则引擎即可**，LLM 润色）」。

**为什么这一版不接模型**：简报是给老板看业务事实的，"润色"收益极小、编造风险不小。
规则生成能保证简报里每个数字都能溯源到具体订单。文档里"LLM 润色"是可选增强，
等真需要口语化表达时再加 —— 而且加的时候也必须带溯源。

**为什么做成「可复制文本」**：文档 §1 明确"不做短信/邮件/LINE/微信推送"，
所以用**人工转发**这种不越界的形式：复制一段贴到群里就行。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Order, User
from .permissions import visibility_filter
from .sla import stay_map, utcnow_naive
from .state_machine import MAIN_FLOW, Role, Status, next_step
from .timeutil import format_display, to_display
from .todo_service import TERMINAL

#: 每节最多列几条（再多就滚屏了，页面里有"查看全部"）
MAX_ITEMS = 8


@dataclass(frozen=True)
class BriefingItem:
    order_no: str
    product: str
    status: str
    stay_label: str
    days: int | None
    overdue: bool
    roles: tuple[str, ...]
    sales: str | None

    @property
    def line(self) -> str:
        parts = [self.order_no, self.product, self.status]
        if self.overdue:
            parts.append(f"⚠️ 已停留 {self.stay_label}")
        elif self.days is not None:
            parts.append(f"停留 {self.stay_label}")
        if self.roles:
            parts.append("等：" + "、".join(self.roles))
        return "｜".join(parts)


@dataclass(frozen=True)
class BriefingSection:
    title: str
    hint: str
    items: tuple[BriefingItem, ...]
    total: int

    @property
    def count(self) -> int:
        return self.total


@dataclass(frozen=True)
class Briefing:
    scope: str
    generated_at: datetime
    headline: str
    sections: tuple[BriefingSection, ...]
    status_counts: tuple[tuple[str, int], ...]
    text: str


# --------------------------------------------------------------------------- #
def _item(order: Order, stay) -> BriefingItem:
    _, roles = next_step(Status(order.status))
    return BriefingItem(
        order_no=order.order_no,
        product=order.product_desc,
        status=order.status,
        stay_label=stay.label if stay else "—",
        days=stay.days if stay else None,
        overdue=bool(stay and stay.overdue),
        roles=roles,
        sales=order.salesperson.display_name if order.salesperson else None,
    )


def _visible(db: Session, user: User) -> list[Order]:
    stmt = select(Order)
    visibility = visibility_filter(user)
    if visibility is not None:
        stmt = stmt.where(visibility)
    return list(db.scalars(stmt.order_by(Order.order_date.desc(), Order.id.desc())).all())


def _section(title: str, hint: str, pairs: list[tuple[Order, object]]) -> BriefingSection:
    items = tuple(_item(order, stay) for order, stay in pairs[:MAX_ITEMS])
    return BriefingSection(title=title, hint=hint, items=items, total=len(pairs))


def _queue(
    orders: list[Order], stays: dict, statuses: tuple[Status, ...]
) -> list[tuple[Order, object]]:
    wanted = {s.value for s in statuses}
    return [(o, stays[o.id]) for o in orders if o.status in wanted]


def _scope_label(user: User) -> str:
    if user.role == Role.ADMIN.value:
        return "全公司"
    return f"{user.display_name}（{user.role_label}）名下"


def build(db: Session, user: User) -> Briefing:
    orders = _visible(db, user)
    stays = stay_map(db, orders, now=utcnow_naive())

    overdue = [(o, stays[o.id]) for o in orders if stays[o.id].overdue]
    overdue.sort(key=lambda pair: pair[1].days or 0, reverse=True)
    exceptions = [(o, stays[o.id]) for o in orders if o.has_exception]
    open_orders = [o for o in orders if o.status not in TERMINAL]

    sections: list[BriefingSection] = [
        _section("卡住了", "停留时间超过该状态合理天数，最需要插手", overdue)
    ]

    if user.role == Role.SALES.value:
        sections.append(
            _section(
                "待你结算",
                "日本端已发货、等平台结算的单，需要你录入结算金额",
                _queue(orders, stays, (Status.WAITING_SETTLEMENT,)),
            )
        )
    elif user.role == Role.PURCHASER.value:
        sections.append(
            _section("待你采购", "销售已提交，还没开始买", _queue(orders, stays, (Status.PENDING_PURCHASE,)))
        )
        sections.append(
            _section("采购中", "你正在处理的单", _queue(orders, stays, (Status.PURCHASING,)))
        )
    elif user.role == Role.WAREHOUSE.value:
        sections.append(
            _section("待你收货", "国内包裹应已寄到上海", _queue(orders, stays, (Status.WAITING_SH_RECEIPT,)))
        )
        sections.append(
            _section(
                "待安排带货",
                "已收货，还没定带货人 / 航班",
                _queue(orders, stays, (Status.RECEIVED_AWAITING_HANDCARRY,)),
            )
        )
        sections.append(
            _section(
                "待登记发货",
                "已安排带货，等登记发货给买家",
                _queue(orders, stays, (Status.HANDCARRY_ARRANGED,)),
            )
        )
    else:
        sections.append(
            _section(
                "待上海收货",
                "最容易卡住的一环",
                _queue(orders, stays, (Status.WAITING_SH_RECEIPT,)),
            )
        )

    sections.append(_section("异常未了结", "少货 / 错货还没处理完", exceptions))

    found: dict[str, int] = {}
    for order in orders:
        found[order.status] = found.get(order.status, 0) + 1
    status_counts = tuple((s.value, found.get(s.value, 0)) for s in MAIN_FLOW)

    draft = Briefing(
        scope=_scope_label(user),
        generated_at=utcnow_naive(),
        headline=_headline(overdue, open_orders, sections),
        sections=tuple(sections),
        status_counts=status_counts,
        text="",
    )
    return replace(draft, text=_render_text(draft))


def _headline(overdue: list, open_orders: list[Order], sections) -> str:
    if overdue:
        worst, stay = overdue[0]
        _, roles = next_step(Status(worst.status))
        who = "、".join(roles) if roles else "相关同事"
        return (
            f"有 {len(overdue)} 单卡住了。最久的是 {worst.order_no}"
            f"（{worst.product_desc}），停在「{worst.status}」{stay.label}，等{who}接手。"
        )
    pending = [s for s in sections if s.title != "卡住了" and s.total]
    if pending:
        detail = "、".join(f"{s.title} {s.total} 单" for s in pending)
        return f"没有卡单。当前待处理：{detail}。"
    if open_orders:
        return f"没有卡单，也没有待你处理的单。进行中还有 {len(open_orders)} 单。"
    return "没有卡单，也没有未结束的订单。"


def _render_text(briefing: Briefing) -> str:
    local = to_display(briefing.generated_at)
    stamp = local.strftime("%Y-%m-%d %H:%M") if local else format_display(briefing.generated_at)
    lines = [f"【日代 ERP · 日报】{stamp}", f"范围：{briefing.scope}", "", briefing.headline]

    for section in briefing.sections:
        lines.append("")
        lines.append(f"■ {section.title}（{section.total} 单）")
        if not section.items:
            lines.append("  · 无")
            continue
        for item in section.items:
            lines.append(f"  · {item.line}")
        if section.total > len(section.items):
            lines.append(f"  … 还有 {section.total - len(section.items)} 单，进系统看完整列表")

    lines.append("")
    lines.append("■ 各状态堆积")
    lines.append("  " + " · ".join(f"{name} {count}" for name, count in briefing.status_counts))
    return "\n".join(lines)
