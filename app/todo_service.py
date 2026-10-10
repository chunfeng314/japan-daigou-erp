"""按角色算待办 —— 首页与 AI 管家共用同一份实现。

为什么抽出来：首页要展示「你该盯什么」，AI 管家要回答「我该做什么」，
两处必须同口径。否则会出现「首页说 3 单、AI 说 5 单」——这种不一致
比没有 AI 更伤信任。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Order, User
from .permissions import visibility_filter
from .sla import overdue_count, stay_map, utcnow_naive
from .state_machine import MAIN_FLOW, Role, Status

TERMINAL = [Status.COMPLETED.value, Status.CANCELLED.value]


@dataclass(frozen=True)
class Todo:
    title: str
    count: int
    hint: str = ""
    status_filter: str | None = None


def count_orders(db: Session, *conditions) -> int:
    stmt = select(func.count()).select_from(Order)
    for condition in conditions:
        stmt = stmt.where(condition)
    return int(db.scalar(stmt) or 0)


def overdue_for_user(db: Session, user: User) -> int:
    """当前用户**可见范围内**已超时的单数。"""
    visibility = visibility_filter(user)
    conditions = [visibility] if visibility is not None else []
    return overdue_count(db, conditions)


def todos_for(db: Session, user: User) -> list[Todo]:
    mine = Order.salesperson_id == user.id
    overdue = overdue_for_user(db, user)

    if user.role == Role.SALES.value:
        return [
            Todo(
                "我的待结算",
                count_orders(db, mine, Order.status == Status.WAITING_SETTLEMENT.value),
                "日本端已发货、等平台结算的单，需要你录入结算金额",
                Status.WAITING_SETTLEMENT.value,
            ),
            Todo(
                "我的进行中",
                count_orders(db, mine, Order.status.notin_(TERMINAL)),
                "我名下所有未结束的订单",
            ),
            Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单"),
            Todo(
                "我的异常未了结",
                count_orders(db, mine, Order.has_exception == 1),
                "少货 / 错货等待处理的单",
            ),
        ]

    if user.role == Role.PURCHASER.value:
        return [
            Todo(
                "待采购",
                count_orders(db, Order.status == Status.PENDING_PURCHASE.value),
                "销售已提交，等你去买",
                Status.PENDING_PURCHASE.value,
            ),
            Todo(
                "采购中",
                count_orders(db, Order.status == Status.PURCHASING.value),
                "你正在处理的单",
                Status.PURCHASING.value,
            ),
            Todo(
                "待上海收货",
                count_orders(db, Order.status == Status.WAITING_SH_RECEIPT.value),
                "货已寄出，盯一下到没到上海",
                Status.WAITING_SH_RECEIPT.value,
            ),
            Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单"),
        ]

    if user.role == Role.WAREHOUSE.value:
        return [
            Todo(
                "待收货",
                count_orders(db, Order.status == Status.WAITING_SH_RECEIPT.value),
                "国内包裹应已寄到上海，等确认收货",
                Status.WAITING_SH_RECEIPT.value,
            ),
            Todo(
                "待安排带货",
                count_orders(db, Order.status == Status.RECEIVED_AWAITING_HANDCARRY.value),
                "已收货但还没定带货人 / 航班",
                Status.RECEIVED_AWAITING_HANDCARRY.value,
            ),
            Todo(
                "待登记发货",
                count_orders(db, Order.status == Status.HANDCARRY_ARRANGED.value),
                "已安排带货，等登记发货给买家",
                Status.HANDCARRY_ARRANGED.value,
            ),
            Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单"),
        ]

    # 管理员：全部 + 超时提醒 + 异常汇总
    return [
        Todo(
            "全部进行中",
            count_orders(db, Order.status.notin_(TERMINAL)),
            "所有未结束的订单",
        ),
        Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单，最需要插手"),
        Todo(
            "异常未了结",
            count_orders(db, Order.has_exception == 1),
            "少货 / 错货等待处理，列表里带红色角标",
        ),
        Todo(
            "待上海收货",
            count_orders(db, Order.status == Status.WAITING_SH_RECEIPT.value),
            "最容易卡住的一环",
            Status.WAITING_SH_RECEIPT.value,
        ),
    ]


def status_overview(db: Session) -> list[tuple[str, int]]:
    """主线各状态的数量（首页「订单全景」用）。"""
    rows = db.execute(select(Order.status, func.count()).group_by(Order.status)).all()
    found = {status: count for status, count in rows}
    return [(s.value, found.get(s.value, 0)) for s in MAIN_FLOW]


def overdue_orders(db: Session, user: User, *, limit: int = 10):
    """可见范围内、已超时的订单（带停留信息），按停留时间倒序。"""
    visibility = visibility_filter(user)
    stmt = select(Order).where(Order.status.notin_(TERMINAL))
    if visibility is not None:
        stmt = stmt.where(visibility)
    orders = list(db.scalars(stmt).all())
    stays = stay_map(db, orders, now=utcnow_naive())
    overdue = [o for o in orders if stays[o.id].overdue]
    overdue.sort(key=lambda o: stays[o.id].days or 0, reverse=True)
    return [(o, stays[o.id]) for o in overdue[:limit]], len(overdue)
