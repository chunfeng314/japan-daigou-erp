"""首页：按角色显示各自待办 + 概览。

待办分两类：
- **按状态数**：这一步该谁做，就出现在谁首页上；
- **卡单提醒**：停留时间超过各状态合理天数（阈值见 `app/sla.py`），
  统计范围受该角色的可见性约束 —— 采购不会看到销售的卡单。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_user
from ..models import Order, User
from ..permissions import visibility_filter
from ..sla import overdue_count
from ..state_machine import MAIN_FLOW, Role, Status
from ..templating import templates

router = APIRouter(tags=["home"])

_TERMINAL = [Status.COMPLETED.value, Status.CANCELLED.value]


@dataclass(frozen=True)
class Todo:
    title: str
    count: int
    hint: str = ""
    status_filter: str | None = None


def _count(db: Session, *conditions) -> int:
    stmt = select(func.count()).select_from(Order)
    for condition in conditions:
        stmt = stmt.where(condition)
    return int(db.scalar(stmt) or 0)


def _overdue(db: Session, user: User) -> int:
    """当前用户**可见范围内**、已超出状态合理停留时间的单数。"""
    visibility = visibility_filter(user)
    conditions = [visibility] if visibility is not None else []
    return overdue_count(db, conditions)


def _status_counts(db: Session) -> list[tuple[str, int]]:
    rows = db.execute(select(Order.status, func.count()).group_by(Order.status)).all()
    found = {status: count for status, count in rows}
    return [(s.value, found.get(s.value, 0)) for s in MAIN_FLOW]


def _todos_for(db: Session, user: User) -> list[Todo]:
    mine = Order.salesperson_id == user.id
    overdue = _overdue(db, user)

    if user.role == Role.SALES.value:
        return [
            Todo(
                "我的待结算",
                _count(db, mine, Order.status == Status.WAITING_SETTLEMENT.value),
                "日本端已发货、等平台结算的单，需要你录入结算金额",
                Status.WAITING_SETTLEMENT.value,
            ),
            Todo(
                "我的进行中",
                _count(db, mine, Order.status.notin_(_TERMINAL)),
                "我名下所有未结束的订单",
            ),
            Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单"),
            Todo(
                "我的异常未了结",
                _count(db, mine, Order.has_exception == 1),
                "少货 / 错货等待处理的单",
            ),
        ]

    if user.role == Role.PURCHASER.value:
        return [
            Todo(
                "待采购",
                _count(db, Order.status == Status.PENDING_PURCHASE.value),
                "销售已提交，等你去买",
                Status.PENDING_PURCHASE.value,
            ),
            Todo(
                "采购中",
                _count(db, Order.status == Status.PURCHASING.value),
                "你正在处理的单",
                Status.PURCHASING.value,
            ),
            Todo(
                "待上海收货",
                _count(db, Order.status == Status.WAITING_SH_RECEIPT.value),
                "货已寄出，盯一下到没到上海",
                Status.WAITING_SH_RECEIPT.value,
            ),
            Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单"),
        ]

    if user.role == Role.WAREHOUSE.value:
        return [
            Todo(
                "待收货",
                _count(db, Order.status == Status.WAITING_SH_RECEIPT.value),
                "国内包裹应已寄到上海，等确认收货",
                Status.WAITING_SH_RECEIPT.value,
            ),
            Todo(
                "待安排带货",
                _count(db, Order.status == Status.RECEIVED_AWAITING_HANDCARRY.value),
                "已收货但还没定带货人 / 航班",
                Status.RECEIVED_AWAITING_HANDCARRY.value,
            ),
            Todo(
                "待登记发货",
                _count(db, Order.status == Status.HANDCARRY_ARRANGED.value),
                "已安排带货，等登记发货给买家",
                Status.HANDCARRY_ARRANGED.value,
            ),
            Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单"),
        ]

    # 管理员：全部 + 超时提醒 + 异常汇总
    return [
        Todo(
            "全部进行中",
            _count(db, Order.status.notin_(_TERMINAL)),
            "所有未结束的订单",
        ),
        Todo("卡单提醒", overdue, "停留时间超过该状态合理天数的单，最需要插手"),
        Todo(
            "异常未了结",
            _count(db, Order.has_exception == 1),
            "少货 / 错货等待处理，列表里带红色角标",
        ),
        Todo(
            "待上海收货",
            _count(db, Order.status == Status.WAITING_SH_RECEIPT.value),
            "最容易卡住的一环",
            Status.WAITING_SH_RECEIPT.value,
        ),
    ]


@router.get("/")
def home(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "todos": _todos_for(db, user),
            "status_counts": _status_counts(db),
            "total_orders": _count(db),
        },
    )
