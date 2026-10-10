"""订单业务动作：订单号生成、建单、商品需求历史累积。"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Order, OrderImage, ProductHistory, StatusLog, User, utcnow
from .state_machine import Status

ORDER_NO_RE = re.compile(r"^(\d{8})-(\d{3,})$")
MAX_ORDER_NO_ATTEMPTS = 5


class OrderNumberError(RuntimeError):
    pass


def generate_order_no(db: Session, on_date: date) -> str:
    """显示编号 `YYYYMMDD-序号`，序号按天从 001 起。"""
    prefix = f"{on_date:%Y%m%d}"
    last = db.scalar(select(func.max(Order.order_no)).where(Order.order_no.like(f"{prefix}-%")))
    seq = 1
    if last:
        matched = ORDER_NO_RE.match(last)
        if matched:
            seq = int(matched.group(2)) + 1
    return f"{prefix}-{seq:03d}"


def create_order(
    db: Session,
    *,
    salesperson: User,
    order_date: date,
    product_desc: str,
    quantity: int,
    sale_amount: float,
    platform: str | None = None,
    shop: str | None = None,
    order_time: str | None = None,
    sale_currency: str = "JPY",
    image_keys: list[str] | None = None,
    image_names: list[str] | None = None,
    note: str | None = None,  # noqa: ARG001  预留给后续"订单备注"
) -> Order:
    """建单并写入截图记录、首条流转记录、商品需求历史。

    订单号用 SAVEPOINT 保护：并发下撞号只回滚这一次尝试，不会把整笔事务搞烂。
    """
    order: Order | None = None
    for _ in range(MAX_ORDER_NO_ATTEMPTS):
        candidate = Order(
            order_no=generate_order_no(db, order_date),
            platform=platform or None,
            shop=shop or None,
            order_date=order_date,
            order_time=order_time or None,
            product_desc=product_desc.strip(),
            quantity=quantity,
            sale_amount=sale_amount,
            sale_currency=sale_currency or "JPY",
            salesperson_id=salesperson.id,
            status=Status.PENDING_PURCHASE.value,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
        except IntegrityError:
            continue
        order = candidate
        break

    if order is None:
        raise OrderNumberError("订单号生成失败：连续多次撞号，请重试")

    keys = image_keys or []
    names = image_names or []
    for index, key in enumerate(keys):
        db.add(
            OrderImage(
                order_id=order.id,
                cos_key=key,
                filename=names[index] if index < len(names) else None,
                uploaded_by=salesperson.id,
            )
        )

    db.add(
        StatusLog(
            order_id=order.id,
            from_status=None,
            to_status=order.status,
            operator_id=salesperson.id,
            note="销售录单",
        )
    )

    remember_product(db, salesperson.id, order.product_desc)
    db.commit()
    db.refresh(order)
    return order


def remember_product(db: Session, user_id: int, product_desc: str) -> None:
    """累积"我最近用过的商品需求"，供录入时快速复用。"""
    desc = (product_desc or "").strip()
    if not desc:
        return
    row = db.scalar(
        select(ProductHistory).where(
            ProductHistory.user_id == user_id, ProductHistory.product_desc == desc
        )
    )
    if row is not None:
        row.use_count += 1
        row.last_used_at = utcnow()
    else:
        db.add(ProductHistory(user_id=user_id, product_desc=desc, use_count=1))


def recent_products(db: Session, user_id: int, *, limit: int = 20, keyword: str | None = None):
    """本人最近用过的商品需求：按最后一次使用时间倒序。"""
    stmt = select(ProductHistory).where(ProductHistory.user_id == user_id)
    if keyword:
        stmt = stmt.where(ProductHistory.product_desc.contains(keyword.strip()))
    stmt = stmt.order_by(ProductHistory.last_used_at.desc()).limit(limit)
    return list(db.scalars(stmt).all())
