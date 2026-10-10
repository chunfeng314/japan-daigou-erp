"""卡单（超时）检测。

文档里管理员首页要「全部 + 超时提醒」，二期也把「卡单检测」列为能力之一。
这里做基础版：给每个状态一个合理停留天数，超了就标红。

⚠️ 阈值是**业务拍脑袋值**，需要老板按实际节奏校一遍；
支持用环境变量 `ERP_SLA_DAYS="待采购=2,采购中=5"` 覆盖，先跑起来再调。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Order, StatusLog
from .state_machine import MAIN_FLOW, Status

#: 各状态「合理停留」天数。终态不检测。
DEFAULT_SLA_DAYS: dict[Status, int] = {
    Status.PENDING_PURCHASE: 1,          # 销售提交后采购应尽快接手
    Status.PURCHASING: 3,                # 国内采购一般当天到两天
    Status.WAITING_SH_RECEIPT: 7,        # 国内快递到上海
    Status.RECEIVED_AWAITING_HANDCARRY: 7,  # 等带货人/航班
    Status.HANDCARRY_ARRANGED: 14,       # 等航班
    Status.SHIPPED: 14,                  # 等平台结算
    Status.WAITING_SETTLEMENT: 7,        # 等销售录结算
}


def _load_sla() -> dict[Status, int]:
    """`ERP_SLA_DAYS="待采购=2,采购中=5"` 覆盖默认值。"""
    raw = os.environ.get("ERP_SLA_DAYS", "").strip()
    table = dict(DEFAULT_SLA_DAYS)
    if not raw:
        return table
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        try:
            table[Status(name.strip())] = int(value.strip())
        except (ValueError, KeyError):
            continue  # 不认的状态名直接忽略，别让配置把人搞崩
    return table


SLA_DAYS: dict[Status, int] = _load_sla()


def sla_days(status: Status) -> int | None:
    return SLA_DAYS.get(status)


def utcnow_naive() -> datetime:
    """库里存的是 naive UTC（SQLite 不保存时区），比较时必须同口径。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@dataclass(frozen=True)
class StayInfo:
    """一张单在当前状态待了多久。"""

    since: datetime | None
    days: int | None
    limit: int | None
    overdue: bool

    @property
    def label(self) -> str:
        if self.days is None:
            return "—"
        if self.limit is None:
            return f"{self.days} 天"
        return f"{self.days} 天 / 限 {self.limit} 天"


def status_since(db: Session, order_ids: Sequence[int]) -> dict[int, datetime]:
    """批量取每张单「进入当前状态」的时间（= 该单最后一条流转记录的时间）。

    取 max(id) 而不是 max(created_at)：同秒写入时后者会歧义。
    """
    ids = [oid for oid in order_ids if oid is not None]
    if not ids:
        return {}
    latest = (
        select(StatusLog.order_id.label("oid"), func.max(StatusLog.id).label("last_id"))
        .where(StatusLog.order_id.in_(ids))
        .group_by(StatusLog.order_id)
        .subquery()
    )
    rows = db.execute(
        select(StatusLog.order_id, StatusLog.created_at).join(
            latest, StatusLog.id == latest.c.last_id
        )
    ).all()
    return {order_id: created for order_id, created in rows}


def stay_info(status: Status, since: datetime | None, *, now: datetime | None = None) -> StayInfo:
    limit = sla_days(status)
    since = _as_naive_utc(since)
    if since is None or limit is None:
        return StayInfo(since, None, limit, False)
    now = now or utcnow_naive()
    elapsed = now - since
    days = int(elapsed.total_seconds() // 86400)
    return StayInfo(since, days, limit, days > limit)


def stay_map(db: Session, orders: Sequence[Order], *, now: datetime | None = None) -> dict[int, StayInfo]:
    """给一批订单算停留信息，列表页直接用。"""
    since_map = status_since(db, [o.id for o in orders])
    now = now or utcnow_naive()
    return {
        order.id: stay_info(Status(order.status), since_map.get(order.id), now=now)
        for order in orders
    }


def overdue_count(db: Session, conditions: Sequence = ()) -> int:
    """未结束且已超时的单数（管理员首页"超时提醒"用）。"""
    stmt = select(Order).where(Order.status.notin_(["已完成", "已取消"]))
    for condition in conditions:
        stmt = stmt.where(condition)
    orders = db.scalars(stmt).all()
    if not orders:
        return 0
    stays = stay_map(db, orders)
    return sum(1 for order in orders if stays[order.id].overdue)


def open_statuses() -> tuple[Status, ...]:
    return MAIN_FLOW
