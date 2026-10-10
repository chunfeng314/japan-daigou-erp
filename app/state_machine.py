"""订单状态机（纯逻辑，不依赖数据库与 Web 框架）。

对应开发文档：
- §2.1 状态定义
- §2.2 状态流转规则（谁能推、能否回退）
- §2.4 异常追踪规则

设计要点：
1. 状态值直接用中文存库（与文档、与老板口头表述一致，查数据时不需要对照表）。
2. 「谁能推」「推之前必须填什么」都写在一张 TRANSITIONS 表里 —— 这是唯一事实来源，
   页面上的按钮、接口层的校验都从它派生，避免"前端能点、后端不认"这类漂移。
3. 回退只给管理员；取消必须填原因，且销售只能取消自己的单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    """角色。值同时是 users.role 里存的字符串。"""

    SALES = "sales"
    PURCHASER = "purchaser"
    WAREHOUSE = "warehouse"
    ADMIN = "admin"


ROLE_LABELS: dict[Role, str] = {
    Role.SALES: "销售",
    Role.PURCHASER: "采购",
    Role.WAREHOUSE: "上海仓",
    Role.ADMIN: "管理员",
}


class Status(str, Enum):
    """订单状态。值即存库值。"""

    PENDING_PURCHASE = "待采购"
    PURCHASING = "采购中"
    WAITING_SH_RECEIPT = "待上海收货"
    RECEIVED_AWAITING_HANDCARRY = "已收货待安排带货"
    HANDCARRY_ARRANGED = "已安排带货"
    SHIPPED = "已发货"
    WAITING_SETTLEMENT = "待结算"
    COMPLETED = "已完成"
    CANCELLED = "已取消"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def values(cls) -> list[str]:
        return [s.value for s in cls]


ALL_STATUSES: tuple[Status, ...] = tuple(Status)

#: 主线顺序（不含「已取消」）。用于展示进度条、计算"回退一级"。
MAIN_FLOW: tuple[Status, ...] = (
    Status.PENDING_PURCHASE,
    Status.PURCHASING,
    Status.WAITING_SH_RECEIPT,
    Status.RECEIVED_AWAITING_HANDCARRY,
    Status.HANDCARRY_ARRANGED,
    Status.SHIPPED,
    Status.WAITING_SETTLEMENT,
    Status.COMPLETED,
)

#: 终态：已完成 / 已取消
TERMINAL_STATUSES: frozenset[Status] = frozenset({Status.COMPLETED, Status.CANCELLED})

#: 「待办」语义的状态 —— 首页按角色列出这些时才有意义
OPEN_STATUSES: frozenset[Status] = frozenset(MAIN_FLOW)

#: 需要是正数的字段（金额 / 数量）。空值或 <= 0 都算不合格。
NUMERIC_FIELDS: frozenset[str] = frozenset({"purchase_price", "final_amount", "sale_amount", "quantity"})

FIELD_LABELS: dict[str, str] = {
    "purchase_price": "采购成本",
    "domestic_tracking_no": "国内快递单号",
    "received_date": "收货日期",
    "handler": "经手人",
    "carrier_person": "带货人",
    "flight_date": "航班日期",
    "ship_date": "发货日期",
    "final_amount": "最终结算金额",
    "cancel_reason": "取消原因",
    "rollback_reason": "回退原因",
}


@dataclass(frozen=True)
class Transition:
    """一条合法流转：谁能推、推进前必须填什么。"""

    src: Status
    dst: Status
    roles: frozenset[Role]
    required_fields: tuple[str, ...] = ()
    label: str = ""
    note: str = ""
    #: 这些角色还必须是「本单销售」才推得动（文档里写"销售（本单销售）"的那些流转）
    owner_roles: frozenset[Role] = frozenset()

    @property
    def role_labels(self) -> tuple[str, ...]:
        return tuple(ROLE_LABELS[r] for r in sorted(self.roles, key=lambda r: r.value))

    @property
    def required_labels(self) -> tuple[str, ...]:
        return tuple(FIELD_LABELS.get(f, f) for f in self.required_fields)


def _t(
    src: Status,
    dst: Status,
    roles: list[Role],
    required: tuple[str, ...] = (),
    label: str = "",
    note: str = "",
    owner_roles: list[Role] | None = None,
) -> Transition:
    return Transition(
        src=src,
        dst=dst,
        roles=frozenset(roles),
        required_fields=required,
        label=label or f"{src.value} → {dst.value}",
        note=note,
        owner_roles=frozenset(owner_roles or ()),
    )


#: 唯一事实来源：文档 §2.2 的流转表
TRANSITIONS: tuple[Transition, ...] = (
    _t(Status.PENDING_PURCHASE, Status.PURCHASING, [Role.PURCHASER, Role.ADMIN], label="开始采购"),
    _t(
        Status.PURCHASING,
        Status.WAITING_SH_RECEIPT,
        [Role.PURCHASER, Role.ADMIN],
        required=("purchase_price", "domestic_tracking_no"),
        label="采购完成",
        note="需先登记采购成本 + 国内快递单号",
    ),
    _t(
        Status.WAITING_SH_RECEIPT,
        Status.RECEIVED_AWAITING_HANDCARRY,
        [Role.WAREHOUSE, Role.ADMIN],
        required=("received_date", "handler"),
        label="确认收货",
        note="需填收货日期 + 经手人",
    ),
    _t(
        Status.RECEIVED_AWAITING_HANDCARRY,
        Status.HANDCARRY_ARRANGED,
        [Role.WAREHOUSE, Role.ADMIN],
        required=("carrier_person", "flight_date"),
        label="安排带货",
        note="需填带货人 + 航班日期",
    ),
    _t(
        Status.HANDCARRY_ARRANGED,
        Status.SHIPPED,
        [Role.WAREHOUSE, Role.ADMIN],
        required=("ship_date",),
        label="登记发货",
        note="需填发货日期（物流单号选填）",
    ),
    _t(Status.SHIPPED, Status.WAITING_SETTLEMENT, [Role.SALES, Role.ADMIN], label="进入结算等待"),
    _t(
        Status.WAITING_SETTLEMENT,
        Status.COMPLETED,
        [Role.SALES, Role.ADMIN],
        required=("final_amount",),
        label="录入结算",
        note="需填最终结算金额；销售只能结算自己的单",
        owner_roles=[Role.SALES],
    ),
)

#: 取消：管理员可取消任意单；销售只能取消自己的单（见 check_transition 的 is_owner）
CANCEL_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.SALES})
CANCEL_REQUIRED_FIELDS: tuple[str, ...] = ("cancel_reason",)

#: 回退：仅管理员
ROLLBACK_ROLES: frozenset[Role] = frozenset({Role.ADMIN})
ROLLBACK_REQUIRED_FIELDS: tuple[str, ...] = ("rollback_reason",)


# --------------------------------------------------------------------------- #
# 查询接口
# --------------------------------------------------------------------------- #
def index_of(status: Status) -> int:
    """主线中的序号；不在主线上（如已取消）返回 -1。"""
    return MAIN_FLOW.index(status) if status in MAIN_FLOW else -1


def next_status(status: Status) -> Status | None:
    """主线上的下一状态；已是终态则返回 None。"""
    idx = index_of(status)
    if idx < 0 or idx + 1 >= len(MAIN_FLOW):
        return None
    return MAIN_FLOW[idx + 1]


def previous_status(status: Status) -> Status | None:
    """主线上的上一状态（回退一级用）；已是第一步则返回 None。"""
    idx = index_of(status)
    if idx <= 0:
        return None
    return MAIN_FLOW[idx - 1]


def transitions_from(status: Status) -> tuple[Transition, ...]:
    return tuple(t for t in TRANSITIONS if t.src == status)


def find_transition(src: Status, dst: Status) -> Transition | None:
    for t in TRANSITIONS:
        if t.src == src and t.dst == dst:
            return t
    return None


def allowed_transitions(status: Status, role: Role, *, is_owner: bool = False) -> tuple[Transition, ...]:
    """给定"当前状态 + 操作人角色"，返回他推得动的流转（供页面渲染按钮）。"""
    result: list[Transition] = []
    for t in transitions_from(status):
        if role not in t.roles:
            continue
        if role in t.owner_roles and not is_owner:
            continue
        result.append(t)

    if status in TERMINAL_STATUSES:
        if role in ROLLBACK_ROLES:
            prev = previous_status(status)
            if prev is not None:
                result.append(
                    Transition(
                        src=status,
                        dst=prev,
                        roles=ROLLBACK_ROLES,
                        required_fields=ROLLBACK_REQUIRED_FIELDS,
                        label=f"回退至「{prev.value}」",
                        note="需填回退原因，写入流转记录",
                    )
                )
    elif role == Role.ADMIN or (role == Role.SALES and is_owner):
        result.append(
            Transition(
                src=status,
                dst=Status.CANCELLED,
                roles=CANCEL_ROLES,
                required_fields=CANCEL_REQUIRED_FIELDS,
                label="取消订单",
                note="必须填取消原因；已发生成本保留显示",
            )
        )
    return tuple(result)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
@dataclass
class TransitionCheck:
    """一次流转校验的结果。allowed=False 时 reason 一定非空。"""

    allowed: bool
    reason: str = ""
    required_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()

    @property
    def missing_labels(self) -> tuple[str, ...]:
        return tuple(FIELD_LABELS.get(f, f) for f in self.missing_fields)


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _check_fields(required: tuple[str, ...], payload: dict) -> tuple[tuple[str, ...], str]:
    """返回（缺失字段, 错误说明）。"""
    missing: list[str] = []
    for name in required:
        if name not in payload or _is_blank(payload[name]):
            missing.append(name)
            continue
        if name in NUMERIC_FIELDS:
            try:
                if float(payload[name]) <= 0:
                    missing.append(name)
            except (TypeError, ValueError):
                missing.append(name)
    if not missing:
        return (), ""
    labels = "、".join(FIELD_LABELS.get(f, f) for f in missing)
    return tuple(missing), f"{labels} 未填写或不合格"


def check_transition(
    *,
    role: Role,
    src: Status,
    dst: Status,
    is_owner: bool = False,
    payload: dict | None = None,
) -> TransitionCheck:
    """判断「把 src 推到 dst」这件事是否允许。

    :param role: 操作人角色
    :param src: 当前状态
    :param dst: 目标状态
    :param is_owner: 操作人是否是本单的销售（决定销售能否取消）
    :param payload: 本次操作提交的字段，用于必填校验
    """
    payload = payload or {}

    if src == dst:
        return TransitionCheck(False, "目标状态与当前状态相同")

    # —— 回退（从终态往回推）：仅管理员 ——
    if src in TERMINAL_STATUSES:
        if role not in ROLLBACK_ROLES:
            return TransitionCheck(False, "只有管理员可以回退订单")
        if index_of(dst) < 0 or index_of(dst) >= index_of(src):
            return TransitionCheck(False, f"不允许从「{src.value}」回退到「{dst.value}」")
        missing, reason = _check_fields(ROLLBACK_REQUIRED_FIELDS, payload)
        if missing:
            return TransitionCheck(False, reason, ROLLBACK_REQUIRED_FIELDS, missing)
        return TransitionCheck(True, required_fields=ROLLBACK_REQUIRED_FIELDS)

    # —— 取消：管理员任意，销售限本单 ——
    if dst == Status.CANCELLED:
        if role == Role.ADMIN or (role == Role.SALES and is_owner):
            missing, reason = _check_fields(CANCEL_REQUIRED_FIELDS, payload)
            if missing:
                return TransitionCheck(False, reason, CANCEL_REQUIRED_FIELDS, missing)
            return TransitionCheck(True, required_fields=CANCEL_REQUIRED_FIELDS)
        if role == Role.SALES:
            return TransitionCheck(False, "销售只能取消自己的订单")
        return TransitionCheck(False, f"{ROLE_LABELS[role]}无权取消订单")

    # —— 常规流转 ——
    transition = find_transition(src, dst)
    if transition is None:
        return TransitionCheck(False, f"不允许的流转：{src.value} → {dst.value}")

    if role not in transition.roles:
        allowed = "、".join(transition.role_labels)
        return TransitionCheck(False, f"当前状态只允许「{allowed}」推进")

    if role in transition.owner_roles and not is_owner:
        return TransitionCheck(False, "该操作只能由本单销售完成")

    missing, reason = _check_fields(transition.required_fields, payload)
    if missing:
        return TransitionCheck(False, reason, transition.required_fields, missing)

    return TransitionCheck(True, required_fields=transition.required_fields)


# --------------------------------------------------------------------------- #
# 异常（少货 / 错货）—— 独立于主状态，见 §2.4
# --------------------------------------------------------------------------- #
class ExceptionType(str, Enum):
    SHORTAGE = "少货"
    WRONG_ITEM = "错货"
    DAMAGED = "破损"
    OTHER = "其他"


class ExceptionStatus(str, Enum):
    PENDING = "待处理"
    IN_PROGRESS = "处理中"
    RESOLVED = "已解决"


#: 未了结的异常（用于订单列表红色角标 / has_exception 标记）
OPEN_EXCEPTION_STATUSES: frozenset[ExceptionStatus] = frozenset(
    {ExceptionStatus.PENDING, ExceptionStatus.IN_PROGRESS}
)


def is_exception_open(status: str | ExceptionStatus) -> bool:
    return ExceptionStatus(status) in OPEN_EXCEPTION_STATUSES
