"""通用状态推进机制 + 异常追踪。

所有环节的骨架完全一样：**校验 → 落环节表 → 改主状态 → 写流转记录**。
所以这里只做两件事：
1. 声明每个环节的**表单字段**（`STAGE_FIELDS`）；
2. 提供统一的 `advance_order()` —— 权限与必填校验一律问 `state_machine`，
   本模块不重复判断"谁能推"，避免两处规则漂移。

异常（少货/错货）独立于主状态，见文档 §2.4。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    JpDelivery,
    JpShipment,
    Order,
    OrderException,
    Purchase,
    Settlement,
    ShReceive,
    StatusLog,
    User,
)
from .state_machine import (
    TERMINAL_STATUSES,
    ExceptionStatus,
    ExceptionType,
    Role,
    Status,
    allowed_transitions,
    check_transition,
    is_exception_open,
)

#: 最终结算与销售金额相差超过这个比例时，要求勾选确认
MISMATCH_THRESHOLD = 0.2


# --------------------------------------------------------------------------- #
# 表单字段声明
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StageField:
    name: str
    label: str
    input_type: str = "text"  # text | number | date | textarea | select | checkbox
    placeholder: str = ""
    hint: str = ""
    default: str = ""
    options: tuple[tuple[str, str], ...] = ()
    required: bool = False  # 渲染时由状态机的 required_fields 覆写，避免两处漂移

    def with_required(self, required: bool) -> StageField:
        return replace(self, required=required)


STAGE_FIELDS: dict[tuple[Status, Status], tuple[StageField, ...]] = {
    (Status.PENDING_PURCHASE, Status.PURCHASING): (),
    (Status.PURCHASING, Status.WAITING_SH_RECEIPT): (
        StageField("purchase_price", "采购成本", "number", placeholder="如 128.5"),
        StageField(
            "purchase_currency",
            "采购币种",
            "select",
            default="CNY",
            options=(("CNY", "CNY 人民币"), ("JPY", "JPY 日元")),
        ),
        StageField("source_platform", "来源平台", placeholder="淘宝 / 拼多多 / 闲鱼"),
        StageField("source_url", "来源链接", placeholder="选填"),
        StageField("domestic_tracking_no", "国内快递单号"),
        StageField("carrier", "承运商", placeholder="顺丰 / 中通…（选填）"),
        StageField("domestic_shipping_fee", "国内运费", "number", placeholder="选填"),
        StageField("other_fee", "其他费用", "number", placeholder="选填"),
        StageField("note", "备注", "textarea"),
    ),
    (Status.WAITING_SH_RECEIPT, Status.RECEIVED_AWAITING_HANDCARRY): (
        StageField("received_date", "收货日期", "date", default="@today"),
        StageField("handler", "经手人"),
        StageField("note", "备注", "textarea"),
    ),
    (Status.RECEIVED_AWAITING_HANDCARRY, Status.HANDCARRY_ARRANGED): (
        StageField("carrier_person", "带货人"),
        StageField("carrier_contact", "联系方式", placeholder="选填"),
        StageField("flight_date", "航班日期", "date"),
        StageField("flight_no", "航班号", placeholder="选填"),
        StageField("from_loc", "出发地", "text", placeholder="如 上海", default="上海"),
        StageField("to_loc", "到达地", "text", placeholder="如 东京", default="东京"),
        StageField("note", "备注", "textarea"),
    ),
    (Status.HANDCARRY_ARRANGED, Status.SHIPPED): (
        StageField("ship_date", "发货日期", "date", default="@today"),
        StageField("carrier", "物流承运", placeholder="选填"),
        StageField("tracking_no", "物流单号", placeholder="选填"),
        StageField("note", "备注", "textarea"),
    ),
    (Status.SHIPPED, Status.WAITING_SETTLEMENT): (),
    (Status.WAITING_SETTLEMENT, Status.COMPLETED): (
        StageField("settle_date", "结算日期", "date", default="@today"),
        StageField("final_amount", "最终结算金额", "number", placeholder="平台实际结算"),
        StageField(
            "currency",
            "币种",
            "select",
            default="JPY",
            options=(("JPY", "JPY 日元"), ("CNY", "CNY 人民币")),
        ),
        StageField("platform_fee", "平台手续费", "number", placeholder="选填"),
        StageField("japan_shipping", "日本国内运费", "number", placeholder="选填"),
        StageField("refund", "退款", "number", placeholder="选填"),
        StageField("other_deduction", "其他扣款", "number", placeholder="选填"),
        StageField("settle_ref", "结算单号", placeholder="选填"),
        StageField("period", "结算期", placeholder="如 2026-10（选填）"),
        StageField(
            "confirm_mismatch",
            "我已核对，确认按此金额结算",
            "checkbox",
            hint=f"仅当与销售金额相差超过 {int(MISMATCH_THRESHOLD * 100)}% 时才需要勾选",
        ),
        StageField("note", "备注", "textarea"),
    ),
}

CANCEL_FIELDS: tuple[StageField, ...] = (
    StageField("cancel_reason", "取消原因", "textarea"),
)

ROLLBACK_FIELDS: tuple[StageField, ...] = (
    StageField("rollback_reason", "回退原因", "textarea"),
)


def stage_fields(src: Status, dst: Status) -> tuple[StageField, ...]:
    if dst == Status.CANCELLED:
        return CANCEL_FIELDS
    if src in TERMINAL_STATUSES:
        return ROLLBACK_FIELDS
    return STAGE_FIELDS.get((src, dst), ())


# --------------------------------------------------------------------------- #
# 逐环节落库
# --------------------------------------------------------------------------- #
def _to_float(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_date(value: object) -> date | None:
    if not value or not str(value).strip():
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _checked(value: object) -> bool:
    return str(value).strip().lower() in {"1", "on", "true", "yes"}


def _get_or_create(db: Session, model, order_id: int):
    row = db.scalar(select(model).where(model.order_id == order_id))
    if row is None:
        row = model(order_id=order_id)
        db.add(row)
    return row


def _persist_purchase(db: Session, order: Order, operator: User, data: dict, *, started: bool) -> None:
    row = _get_or_create(db, Purchase, order.id)
    row.purchaser_id = operator.id
    if started:
        row.purchase_status = "采购中"
        return
    row.purchase_status = "已完成"
    row.purchase_price = _to_float(data.get("purchase_price"))
    row.purchase_currency = _clean(data.get("purchase_currency")) or "CNY"
    row.source_platform = _clean(data.get("source_platform"))
    row.source_url = _clean(data.get("source_url"))
    row.domestic_tracking_no = _clean(data.get("domestic_tracking_no"))
    row.carrier = _clean(data.get("carrier"))
    row.domestic_shipping_fee = _to_float(data.get("domestic_shipping_fee"))
    row.other_fee = _to_float(data.get("other_fee"))
    row.note = _clean(data.get("note"))


def _persist_receive(db: Session, order: Order, data: dict) -> None:
    row = _get_or_create(db, ShReceive, order.id)
    row.received = 1
    row.received_date = _to_date(data.get("received_date"))
    row.handler = _clean(data.get("handler"))
    row.note = _clean(data.get("note"))


def _persist_shipment(db: Session, order: Order, data: dict) -> None:
    row = _get_or_create(db, JpShipment, order.id)
    row.arranged = 1
    row.carrier_person = _clean(data.get("carrier_person"))
    row.carrier_contact = _clean(data.get("carrier_contact"))
    row.flight_date = _to_date(data.get("flight_date"))
    row.flight_no = _clean(data.get("flight_no"))
    row.from_loc = _clean(data.get("from_loc"))
    row.to_loc = _clean(data.get("to_loc"))
    row.note = _clean(data.get("note"))


def _persist_delivery(db: Session, order: Order, operator: User, data: dict) -> None:
    row = _get_or_create(db, JpDelivery, order.id)
    row.ship_date = _to_date(data.get("ship_date"))
    row.carrier = _clean(data.get("carrier"))
    row.tracking_no = _clean(data.get("tracking_no"))
    row.recorded_by = operator.id
    row.note = _clean(data.get("note"))


def _persist_settlement(db: Session, order: Order, operator: User, data: dict) -> None:
    row = _get_or_create(db, Settlement, order.id)
    row.settle_date = _to_date(data.get("settle_date")) or date.today()
    row.final_amount = _to_float(data.get("final_amount")) or 0.0
    row.currency = _clean(data.get("currency")) or "JPY"
    row.platform_fee = _to_float(data.get("platform_fee"))
    row.japan_shipping = _to_float(data.get("japan_shipping"))
    row.refund = _to_float(data.get("refund"))
    row.other_deduction = _to_float(data.get("other_deduction"))
    row.settle_status = "已完成"
    row.entered_by = operator.id
    row.settle_ref = _clean(data.get("settle_ref"))
    row.period = _clean(data.get("period"))
    row.note = _clean(data.get("note"))


def _persist_stage(
    db: Session, src: Status, dst: Status, order: Order, operator: User, data: dict
) -> None:
    key = (src, dst)
    if key == (Status.PENDING_PURCHASE, Status.PURCHASING):
        _persist_purchase(db, order, operator, data, started=True)
    elif key == (Status.PURCHASING, Status.WAITING_SH_RECEIPT):
        _persist_purchase(db, order, operator, data, started=False)
    elif key == (Status.WAITING_SH_RECEIPT, Status.RECEIVED_AWAITING_HANDCARRY):
        _persist_receive(db, order, data)
    elif key == (Status.RECEIVED_AWAITING_HANDCARRY, Status.HANDCARRY_ARRANGED):
        _persist_shipment(db, order, data)
    elif key == (Status.HANDCARRY_ARRANGED, Status.SHIPPED):
        _persist_delivery(db, order, operator, data)
    elif key == (Status.WAITING_SETTLEMENT, Status.COMPLETED):
        _persist_settlement(db, order, operator, data)


def _pre_check(
    src: Status, dst: Status, order: Order, data: dict
) -> tuple[bool, str]:
    """落库前的业务规则（状态机管不到的）。"""
    if (src, dst) == (Status.WAITING_SETTLEMENT, Status.COMPLETED):
        final = _to_float(data.get("final_amount"))
        sale = float(order.sale_amount or 0)
        if final is not None and sale > 0:
            gap = abs(final - sale) / sale
            if gap > MISMATCH_THRESHOLD and not _checked(data.get("confirm_mismatch")):
                return False, (
                    f"最终结算金额 {final:,.0f} 与销售金额 {sale:,.0f} 相差 "
                    f"{gap * 100:.0f}%（超过 {int(MISMATCH_THRESHOLD * 100)}%）。"
                    "请核对后勾选「我已核对」再提交。"
                )
    return True, ""


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
@dataclass
class AdvanceResult:
    ok: bool
    message: str = ""


def advance_order(
    db: Session,
    *,
    order: Order,
    to_status: Status,
    operator: User,
    data: dict | None = None,
) -> AdvanceResult:
    """把订单推进到 to_status（含取消与回退）。校验一律走状态机。"""
    data = data or {}
    src = Status(order.status)
    is_owner = order.salesperson_id == operator.id

    check = check_transition(
        role=operator.role_enum, src=src, dst=to_status, is_owner=is_owner, payload=data
    )
    if not check.allowed:
        return AdvanceResult(False, check.reason)

    passed, reason = _pre_check(src, to_status, order, data)
    if not passed:
        return AdvanceResult(False, reason)

    _persist_stage(db, src, to_status, order, operator, data)
    order.status = to_status.value

    note = _clean(data.get("note")) or ""
    if to_status == Status.CANCELLED:
        order.cancel_reason = _clean(data.get("cancel_reason"))
    elif src in TERMINAL_STATUSES:
        note = _clean(data.get("rollback_reason")) or note

    db.add(
        StatusLog(
            order_id=order.id,
            from_status=src.value,
            to_status=to_status.value,
            operator_id=operator.id,
            note=note or None,
        )
    )
    db.commit()
    db.refresh(order)
    return AdvanceResult(True, f"已推进到「{to_status.value}」")


def options_for(db: Session, user: User, order: Order) -> list[dict]:
    """当前用户在**这笔订单**上能做的操作，供详情页渲染操作面板。"""
    src = Status(order.status)
    is_owner = order.salesperson_id == user.id

    options: list[dict] = []
    for transition in allowed_transitions(src, user.role_enum, is_owner=is_owner):
        required = set(transition.required_fields)
        fields = tuple(
            replace(field, default=date.today().isoformat()) if field.default == "@today" else field
            for field in (
                field.with_required(field.name in required)
                for field in stage_fields(transition.src, transition.dst)
            )
        )
        options.append(
            {
                "dst": transition.dst.value,
                "label": transition.label,
                "note": transition.note,
                "fields": fields,
                "is_cancel": transition.dst == Status.CANCELLED,
                "is_rollback": transition.src in TERMINAL_STATUSES,
            }
        )
    return options


# --------------------------------------------------------------------------- #
# 异常追踪（少货 / 错货）
# --------------------------------------------------------------------------- #
def add_exception(
    db: Session, *, order: Order, operator: User, data: dict
) -> tuple[bool, str]:
    exception_type = _clean(data.get("exception_type"))
    if exception_type not in {t.value for t in ExceptionType}:
        return False, "请选择异常类型"

    db.add(
        OrderException(
            order_id=order.id,
            exception_type=exception_type,
            found_stage=_clean(data.get("found_stage")),
            detail=_clean(data.get("detail")),
            qty_affected=int(data["qty_affected"]) if str(data.get("qty_affected", "")).strip().isdigit() else None,
            resolution=_clean(data.get("resolution")),
            ex_status=ExceptionStatus.PENDING.value,
            handled_by=operator.id,
            note=_clean(data.get("note")),
        )
    )
    order.has_exception = 1
    db.commit()
    return True, "已登记异常"


def resolve_exception(
    db: Session,
    *,
    order: Order,
    exception_id: int,
    operator: User,
    data: dict | None = None,
) -> tuple[bool, str]:
    data = data or {}
    row = db.get(OrderException, exception_id)
    if row is None or row.order_id != order.id:
        return False, "异常记录不存在"

    row.resolution = _clean(data.get("resolution")) or row.resolution
    row.note = _clean(data.get("note")) or row.note
    row.ex_status = ExceptionStatus.RESOLVED.value
    row.handled_by = operator.id
    row.resolved_at = datetime.now()

    db.flush()
    refresh_exception_flag(db, order)
    db.commit()
    return True, "异常已了结"


def refresh_exception_flag(db: Session, order: Order) -> None:
    """按未了结异常的数量维护 orders.has_exception 冗余标记。"""
    rows = db.scalars(
        select(OrderException).where(OrderException.order_id == order.id)
    ).all()
    order.has_exception = 1 if any(is_exception_open(r.ex_status) for r in rows) else 0
