"""状态推进与异常追踪的写接口。

一个通用端点 `POST /orders/{id}/transition` 承接**全部**流转（含取消与回退）：
目标状态由表单里的 `to_status` 指定，能不能推、必填什么全问状态机。
这样以后加环节只要改 `state_machine` + `transition_service` 的字段声明，不用加路由。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import Forbidden, require_user
from ..models import Order, OrderException, User
from ..permissions import can_view_order
from ..state_machine import Role, Status
from ..transition_service import add_exception, advance_order, resolve_exception

router = APIRouter(prefix="/orders", tags=["stages"])


def _back(order_id: int, *, msg: str | None = None, error: str | None = None) -> RedirectResponse:
    query = []
    if msg:
        query.append(f"msg={quote(msg)}")
    if error:
        query.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return RedirectResponse(f"/orders/{order_id}{suffix}", status_code=303)


def _load_order(db: Session, user: User, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or not can_view_order(db, user, order):
        raise Forbidden("订单不存在或你没有权限操作")
    return order


@router.post("/{order_id}/transition")
async def order_transition(
    order_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    order = _load_order(db, user, order_id)
    form = await request.form()

    raw_status = str(form.get("to_status") or "")
    try:
        to_status = Status(raw_status)
    except ValueError:
        return _back(order_id, error=f"未知的目标状态：{raw_status}")

    data = {key: value for key, value in form.items() if key != "to_status"}
    result = advance_order(db, order=order, to_status=to_status, operator=user, data=data)
    if result.ok:
        return _back(order_id, msg=result.message)
    return _back(order_id, error=result.message)


@router.post("/{order_id}/exceptions")
async def create_exception(
    order_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    order = _load_order(db, user, order_id)
    form = await request.form()
    ok, message = add_exception(db, order=order, operator=user, data=dict(form.items()))
    return _back(order_id, msg=message) if ok else _back(order_id, error=message)


@router.post("/{order_id}/exceptions/{exception_id}/resolve")
async def resolve_order_exception(
    order_id: int,
    exception_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    order = _load_order(db, user, order_id)
    if user.role != Role.ADMIN.value:
        raise Forbidden("异常的关闭只有管理员可以做")

    form = await request.form()
    ok, message = resolve_exception(
        db, order=order, exception_id=exception_id, operator=user, data=dict(form.items())
    )
    return _back(order_id, msg=message) if ok else _back(order_id, error=message)


@router.post("/{order_id}/exceptions/{exception_id}/reopen")
def reopen_order_exception(
    order_id: int,
    exception_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    order = _load_order(db, user, order_id)
    if user.role != Role.ADMIN.value:
        raise Forbidden("异常的重新打开只有管理员可以做")

    row = db.get(OrderException, exception_id)
    if row is None or row.order_id != order.id:
        return _back(order_id, error="异常记录不存在")

    row.ex_status = "待处理"
    row.resolved_at = None
    db.flush()
    from ..transition_service import refresh_exception_flag

    refresh_exception_flag(db, order)
    db.commit()
    return _back(order_id, msg="异常已重新打开")
