"""AI 管家的工具层 —— 安全红线的落点。

开发文档 §9 原话：**权限过滤在工具层执行（不能靠提示词）**。

所以这里的每个工具都必须接收 `AgentContext`（内含当前用户），
任何数据访问都要过 `permissions` 的可见性过滤。模型能决定的只有
「调用哪个工具、传什么参数」，**决定不了「能不能看到别人的单」**。

另一条设计：查不到别人的单时返回「没查到你名下的 X」，而不是「无权限」——
后者等于承认"这张单存在"，本身就是信息泄露。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Order, User
from ..permissions import visibility_filter
from ..sla import stay_map
from ..state_machine import Status, next_step
from ..todo_service import overdue_for_user, todos_for

MAX_LIMIT = 50


@dataclass(frozen=True)
class AgentContext:
    """工具执行上下文。**唯一的用户入口** —— 工具拿不到别的用户。"""

    db: Session
    user: User


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    summary: str
    data: Any = None
    citations: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., ToolResult]
    read_only: bool = True
    #: 预留给后续写工具：写操作必须经接口层二次确认（文档 §9）
    requires_confirmation: bool = False


# --------------------------------------------------------------------------- #
# 内部小工具
# --------------------------------------------------------------------------- #
def _order_no_variants(raw: str) -> list[str]:
    """把用户可能写的形式归一：`20261010-001` / `20261010001` / `2026 1010-001`。"""
    text = (raw or "").strip().replace(" ", "")
    variants = [text]
    if text.isdigit() and len(text) > 3:
        variants.append(f"{text[:8]}-{text[8:]}")
    return [v for v in dict.fromkeys(variants) if v]


def _next_step(status: Status) -> tuple[Status | None, tuple[str, ...]]:
    """主线上的下一步 + 该谁做（统一实现见 state_machine.next_step）。"""
    return next_step(status)


def _visible_orders_stmt(ctx: AgentContext):
    """所有订单查询的**唯一**入口：可见性过滤在这里强制生效。"""
    stmt = select(Order)
    visibility = visibility_filter(ctx.user)
    if visibility is not None:
        stmt = stmt.where(visibility)
    return stmt


def _describe(order: Order, stay) -> dict:
    status = Status(order.status)
    nxt, roles = _next_step(status)
    return {
        "order_no": order.order_no,
        "product": order.product_desc,
        "quantity": order.quantity,
        "amount": f"{order.sale_amount:,.0f} {order.sale_currency}",
        "status": status.value,
        "order_date": order.order_date.isoformat(),
        "sales": order.salesperson.display_name if order.salesperson else None,
        "stay_days": stay.days if stay else None,
        "stay_label": stay.label if stay else "—",
        "overdue": bool(stay and stay.overdue),
        "has_exception": bool(order.has_exception),
        "next_status": nxt.value if nxt else None,
        "next_roles": list(roles),
    }


def _one_line(info: dict) -> str:
    parts = [f"{info['order_no']}（{info['status']}）", info["product"]]
    if info["overdue"]:
        parts.append(f"⚠️ 已超时：{info['stay_label']}")
    elif info["stay_days"] is not None:
        parts.append(f"停留 {info['stay_label']}")
    if info["has_exception"]:
        parts.append("有未了结异常")
    if info["next_roles"]:
        parts.append("下一步轮到：" + "、".join(info["next_roles"]))
    return "，".join(parts)


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def _tool_find_order(ctx: AgentContext, order_no: str) -> ToolResult:
    matched: Order | None = None
    for variant in _order_no_variants(str(order_no)):
        stmt = _visible_orders_stmt(ctx).where(Order.order_no == variant)
        matched = ctx.db.scalars(stmt).first()
        if matched:
            break
    if matched is None:
        # 刻意不区分「不存在」与「无权限」——后者会泄露订单存在性
        return ToolResult(
            ok=False,
            summary=f"没有查到你名下的订单「{order_no}」。如果这不是你的单，你看不到它。",
            error="not_visible",
        )

    info = _describe(matched, stay_map(ctx.db, [matched]).get(matched.id))
    lines = [_one_line(info)]
    if info["next_status"]:
        lines.append(f"下一步：推进到「{info['next_status']}」，由「{'、'.join(info['next_roles'])}」操作。")
    else:
        lines.append("这单已经结束，没有下一步。")
    return ToolResult(True, "\n".join(lines), data=info, citations=(info["order_no"],))


def _tool_list_orders(
    ctx: AgentContext,
    status: str | None = None,
    only_overdue: bool = False,
    limit: int = 10,
) -> ToolResult:
    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    stmt = _visible_orders_stmt(ctx)

    if status:
        if status not in Status.values():
            return ToolResult(False, f"没有「{status}」这个状态。", error="bad_status")
        stmt = stmt.where(Order.status == status)

    orders = list(ctx.db.scalars(stmt.order_by(Order.order_date.desc(), Order.id.desc())).all())
    stays = stay_map(ctx.db, orders)
    if only_overdue:
        orders = [o for o in orders if stays[o.id].overdue]

    total = len(orders)
    shown = orders[:limit]
    if not shown:
        label = status or ("超时的" if only_overdue else "")
        return ToolResult(True, f"你名下没有{label}订单。", data=[])

    infos = [_describe(o, stays[o.id]) for o in shown]
    body = "\n".join(f"{i+1}. {_one_line(info)}" for i, info in enumerate(infos))
    tail = f"（共 {total} 单，先列 {len(shown)} 单）" if total > len(shown) else f"（共 {total} 单）"
    return ToolResult(
        True, f"{body}\n{tail}", data=infos, citations=tuple(i["order_no"] for i in infos)
    )


def _tool_count_orders(ctx: AgentContext, status: str | None = None) -> ToolResult:
    orders = list(ctx.db.scalars(_visible_orders_stmt(ctx)).all())
    if status:
        if status not in Status.values():
            return ToolResult(False, f"没有「{status}」这个状态。", error="bad_status")
        orders = [o for o in orders if o.status == status]
    label = f"「{status}」的" if status else "你能看到的全部"
    return ToolResult(True, f"{label}订单共 {len(orders)} 单。", data={"count": len(orders)})


def _tool_my_todos(ctx: AgentContext) -> ToolResult:
    todos = todos_for(ctx.db, ctx.user)
    lines = [f"- {t.title}：{t.count} 单" for t in todos]
    return ToolResult(
        True,
        f"你（{ctx.user.display_name}）当前待办：\n" + "\n".join(lines),
        data=[{"title": t.title, "count": t.count} for t in todos],
    )


def _tool_overdue_orders(ctx: AgentContext, limit: int = 10) -> ToolResult:
    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    count = overdue_for_user(ctx.db, ctx.user)
    if count == 0:
        return ToolResult(True, "你名下没有超时的单。", data=[])

    result = _tool_list_orders(ctx, only_overdue=True, limit=limit)
    return ToolResult(
        result.ok,
        f"有 {count} 单超时：\n{result.summary}",
        data=result.data,
        citations=result.citations,
    )


def _tool_order_progress(ctx: AgentContext, order_no: str) -> ToolResult:
    """回答「到哪一步了」：含已登记的环节信息。"""
    base = _tool_find_order(ctx, order_no)
    if not base.ok:
        return base

    from ..models import JpDelivery, JpShipment, Purchase, Settlement, ShReceive

    order = ctx.db.scalars(
        _visible_orders_stmt(ctx).where(Order.order_no == base.data["order_no"])
    ).first()
    assert order is not None  # 上面刚查到，且可见性条件相同

    marks: list[str] = []
    purchase = ctx.db.scalar(select(Purchase).where(Purchase.order_id == order.id))
    if purchase is not None and (purchase.purchase_price is not None or purchase.purchaser_id):
        marks.append("已采购")
    if ctx.db.scalar(select(ShReceive).where(ShReceive.order_id == order.id)) is not None:
        marks.append("已收货")
    if ctx.db.scalar(select(JpShipment).where(JpShipment.order_id == order.id)) is not None:
        marks.append("已安排带货")
    if ctx.db.scalar(select(JpDelivery).where(JpDelivery.order_id == order.id)) is not None:
        marks.append("已发货给买家")
    if ctx.db.scalar(select(Settlement).where(Settlement.order_id == order.id)) is not None:
        marks.append("已结算")

    stage = "已完成阶段：" + "、".join(marks) if marks else "还没有登记任何环节信息"
    return ToolResult(
        base.ok,
        f"{base.summary}\n{stage}。",
        data={**base.data, "registered_stages": marks},
        citations=base.citations,
    )


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="find_order",
            description="按订单号查一张订单的当前状态、停留天数、异常与下一步该谁做。查不到会明确说查不到。",
            parameters={
                "type": "object",
                "properties": {"order_no": {"type": "string", "description": "订单号，如 20261010-001"}},
                "required": ["order_no"],
            },
            handler=_tool_find_order,
        ),
        ToolSpec(
            name="order_progress",
            description="回答「这单到哪一步了」：状态 + 已登记的环节（采购/收货/带货/发货/结算）。",
            parameters={
                "type": "object",
                "properties": {"order_no": {"type": "string"}},
                "required": ["order_no"],
            },
            handler=_tool_order_progress,
        ),
        ToolSpec(
            name="list_orders",
            description="列出你能看到的订单，可按状态过滤、也可只看超时的。",
            parameters={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": Status.values()},
                    "only_overdue": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
            handler=_tool_list_orders,
        ),
        ToolSpec(
            name="count_orders",
            description="统计你能看到的订单数量，可按状态过滤。",
            parameters={
                "type": "object",
                "properties": {"status": {"type": "string", "enum": Status.values()}},
            },
            handler=_tool_count_orders,
        ),
        ToolSpec(
            name="my_todos",
            description="当前登录人的待办汇总（与首页显示的完全一致）。",
            parameters={"type": "object", "properties": {}},
            handler=_tool_my_todos,
        ),
        ToolSpec(
            name="overdue_orders",
            description="列出停留时间超出该状态合理天数的订单（卡单）。",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            handler=_tool_overdue_orders,
        ),
    )
}


# --------------------------------------------------------------------------- #
# 执行入口
# --------------------------------------------------------------------------- #
def describe_tools() -> list[dict]:
    """给模型看的工具清单（OpenAI tools 格式）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in TOOLS.values()
        if spec.read_only
    ]


def run_tool(ctx: AgentContext, name: str, arguments: dict | None = None) -> ToolResult:
    """执行一个工具。未知工具 / 参数不符一律拒绝，不让异常穿透到编排层。"""
    spec = TOOLS.get(name)
    if spec is None:
        return ToolResult(False, f"没有「{name}」这个能力。", error="unknown_tool")

    arguments = dict(arguments or {})
    allowed = set(spec.parameters.get("properties", {}))
    extra = set(arguments) - allowed
    if extra:
        return ToolResult(False, f"{name} 不接受的参数：{'、'.join(sorted(extra))}", error="bad_params")

    try:
        return spec.handler(ctx, **arguments)
    except TypeError as exc:
        return ToolResult(False, f"{name} 参数不对：{exc}", error="bad_params")
