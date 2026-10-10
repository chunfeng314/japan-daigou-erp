"""订单可见性 —— 实现文档 §2.3 权限矩阵里「查看订单」那一行。

| 角色 | 可见范围 |
|---|---|
| 销售 | 自己的单 |
| 采购 | 待采购 + 自己经手过的单 |
| 上海仓 | 与收货/带货/发货相关的单；**按单号或快递单搜索时可查全库**（文档：按单号/快递单查询） |
| 管理员 | 全部 |
"""

from __future__ import annotations

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.sql.elements import ColumnElement

from .models import Order, Purchase, User
from .state_machine import Role, Status

#: 上海仓日常打交道的状态
WAREHOUSE_STATUSES: tuple[Status, ...] = (
    Status.WAITING_SH_RECEIPT,
    Status.RECEIVED_AWAITING_HANDCARRY,
    Status.HANDCARRY_ARRANGED,
    Status.SHIPPED,
)


def _purchased_by(user_id: int) -> ColumnElement[bool]:
    return exists().where(and_(Purchase.order_id == Order.id, Purchase.purchaser_id == user_id))


def visibility_filter(user: User, *, warehouse_search: bool = False) -> ColumnElement[bool] | None:
    """返回一个 where 条件；`None` 表示不限制。"""
    role = user.role

    if role == Role.ADMIN.value:
        return None

    if role == Role.SALES.value:
        return Order.salesperson_id == user.id

    if role == Role.PURCHASER.value:
        return or_(Order.status == Status.PENDING_PURCHASE.value, _purchased_by(user.id))

    # 上海仓
    if warehouse_search:
        return None
    return Order.status.in_([s.value for s in WAREHOUSE_STATUSES])


def can_view_order(db, user: User, order: Order, *, warehouse_search: bool = False) -> bool:
    """先按角色做快速判断，避免为一个订单详情也去拼子查询。

    上海仓：列表默认只给"待处理"的单，但**详情页允许按订单号/单号查到任意单**
    （他的活儿是按单号查货，只读即可）——所以这里直接放行。
    """
    role = user.role
    if role in (Role.ADMIN.value, Role.WAREHOUSE.value):
        return True
    if role == Role.SALES.value:
        return order.salesperson_id == user.id

    # 采购
    if order.status == Status.PENDING_PURCHASE.value:
        return True
    return (
        db.scalar(
            select(Purchase.id).where(
                Purchase.order_id == order.id, Purchase.purchaser_id == user.id
            )
        )
        is not None
    )


def can_edit_order(user: User, order: Order) -> bool:
    """编辑限未完成订单；销售只能改自己的。"""
    if order.status in {Status.COMPLETED.value, Status.CANCELLED.value}:
        return False
    if user.role == Role.ADMIN.value:
        return True
    return user.role == Role.SALES.value and order.salesperson_id == user.id


def can_create_order(user: User) -> bool:
    return user.role in {Role.SALES.value, Role.ADMIN.value}
