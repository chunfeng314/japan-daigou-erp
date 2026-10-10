"""数据模型 —— 对应开发文档 §3 数据库设计。

约定：
- 一单一品：orders 不建明细子表；买家一单多商品 → 拆多单。
- 金额分币种各记各的（销售/结算=日元，采购=人民币），不自动换汇、不算利润。
- 每个环节独立一张表，订单主表只存主线状态；环节字段各自扩展互不影响。
- 异常独立成表（order_exceptions），主表用 has_exception 冗余标记做列表角标。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .state_machine import Role, Status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('sales','purchaser','warehouse','admin')", name="ck_users_role"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    @property
    def role_enum(self) -> Role:
        return Role(self.role)

    @property
    def role_label(self) -> str:
        from .state_machine import ROLE_LABELS

        return ROLE_LABELS[self.role_enum]


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"status IN ({', '.join(repr(s.value) for s in Status)})", name="ck_orders_status"),
        CheckConstraint("quantity > 0", name="ck_orders_quantity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    platform: Mapped[str | None] = mapped_column(String(64))
    shop: Mapped[str | None] = mapped_column(String(64))
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    order_time: Mapped[str | None] = mapped_column(String(8))

    product_desc: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    sale_amount: Mapped[float] = mapped_column(Float, nullable=False)
    sale_currency: Mapped[str] = mapped_column(String(8), default="JPY", nullable=False)

    salesperson_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    #: 采购负责人：销售建单时可指定；为空 = 未分配，采购先接先得。
    #: 与 `purchases.purchaser_id`（实际经办人）可以不同 —— 指派的休假时可以换人接。
    assigned_purchaser_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    assigned_purchaser: Mapped["User | None"] = relationship(foreign_keys=[assigned_purchaser_id])
    status: Mapped[str] = mapped_column(
        String(24), default=Status.PENDING_PURCHASE.value, nullable=False, index=True
    )
    has_exception: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    salesperson: Mapped[User | None] = relationship("User", foreign_keys=[salesperson_id])
    images: Mapped[list["OrderImage"]] = relationship(
        "OrderImage", back_populates="order", cascade="all, delete-orphan"
    )

    @property
    def status_enum(self) -> Status:
        return Status(self.status)

    @property
    def status_label(self) -> str:
        return self.status


class OrderImage(Base):
    __tablename__ = "order_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    cos_key: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str | None] = mapped_column(String(255))
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    order: Mapped[Order] = relationship("Order", back_populates="images")


class ProductHistory(Base):
    """商品需求历史：按本人最近使用排序，录入时下拉复用。"""

    __tablename__ = "product_history"
    __table_args__ = (UniqueConstraint("user_id", "product_desc", name="uq_product_history_user_desc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_desc: Mapped[str] = mapped_column(Text)
    use_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    purchaser_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    purchase_status: Mapped[str | None] = mapped_column(String(16))

    purchase_price: Mapped[float | None] = mapped_column(Float)
    purchase_currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    source_platform: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(String(1024))
    domestic_tracking_no: Mapped[str | None] = mapped_column(String(64))
    carrier: Mapped[str | None] = mapped_column(String(64))
    domestic_shipping_fee: Mapped[float | None] = mapped_column(Float)
    other_fee: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ShReceive(Base):
    """上海仓收货登记。"""

    __tablename__ = "sh_receives"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    received: Mapped[int | None] = mapped_column(Integer)
    received_date: Mapped[date | None] = mapped_column(Date)
    handler: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)


class JpShipment(Base):
    """带货安排（按订单记，不建"航班"独立实体）。"""

    __tablename__ = "jp_shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    arranged: Mapped[int | None] = mapped_column(Integer)
    carrier_person: Mapped[str | None] = mapped_column(String(64))
    carrier_contact: Mapped[str | None] = mapped_column(String(64))
    flight_date: Mapped[date | None] = mapped_column(Date)
    flight_no: Mapped[str | None] = mapped_column(String(32))
    from_loc: Mapped[str | None] = mapped_column(String(64))
    to_loc: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)


class JpDelivery(Base):
    """日本端发货给买家（v1.1 新增：状态机「已发货」的落地表）。"""

    __tablename__ = "jp_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    ship_date: Mapped[date | None] = mapped_column(Date, nullable=False)
    carrier: Mapped[str | None] = mapped_column(String(64))
    tracking_no: Mapped[str | None] = mapped_column(String(64))
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OrderException(Base):
    """少货 / 错货 / 破损异常追踪（v1.1 新增）。不改主状态，独立追踪。"""

    __tablename__ = "order_exceptions"
    __table_args__ = (
        CheckConstraint(
            "exception_type IN ('少货','错货','破损','其他')", name="ck_order_exceptions_type"
        ),
        CheckConstraint(
            "ex_status IN ('待处理','处理中','已解决')", name="ck_order_exceptions_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    exception_type: Mapped[str] = mapped_column(String(8), nullable=False)
    found_stage: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
    qty_affected: Mapped[int | None] = mapped_column(Integer)
    resolution: Mapped[str | None] = mapped_column(String(32))
    ex_status: Mapped[str] = mapped_column(String(8), default="待处理", nullable=False, index=True)
    handled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    settle_date: Mapped[date] = mapped_column(Date, nullable=False)
    final_amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="JPY", nullable=False)
    platform_fee: Mapped[float | None] = mapped_column(Float)
    japan_shipping: Mapped[float | None] = mapped_column(Float)
    refund: Mapped[float | None] = mapped_column(Float)
    other_deduction: Mapped[float | None] = mapped_column(Float)
    settle_status: Mapped[str | None] = mapped_column(String(16))
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    settle_ref: Mapped[str | None] = mapped_column(String(64))
    period: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)


class StatusLog(Base):
    """状态流转记录（审计：谁在何时把单推到哪一步）。"""

    __tablename__ = "status_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


__all__ = [
    "User",
    "Order",
    "OrderImage",
    "ProductHistory",
    "Purchase",
    "ShReceive",
    "JpShipment",
    "JpDelivery",
    "OrderException",
    "Settlement",
    "StatusLog",
    "utcnow",
]
