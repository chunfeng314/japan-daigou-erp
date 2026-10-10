"""订单：列表 / 录单 / 详情 / 编辑。

D2~3 范围：销售录入与查看。状态推进（采购/收货/带货/发货/结算/异常）在 D4 之后逐步上线。
"""

from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..deps import Forbidden, require_user
from ..models import Order, OrderImage, Purchase, StatusLog, User
from ..order_service import create_order, recent_products
from ..permissions import can_create_order, can_edit_order, can_view_order, visibility_filter
from ..state_machine import MAIN_FLOW, Role, Status
from ..storage import get_storage
from ..templating import templates

router = APIRouter(prefix="/orders", tags=["orders"])
api_router = APIRouter(prefix="/api", tags=["orders-api"])

PAGE_SIZE = 20


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _validate_order_form(
    order_date: str, product_desc: str, quantity: str, sale_amount: str
) -> tuple[date | None, str, int, float, list[str]]:
    """录单 / 改单共用的字段校验。"""
    errors: list[str] = []

    parsed_date = _parse_date(order_date)
    if parsed_date is None:
        errors.append("订单日期格式不对（应为 YYYY-MM-DD）")

    desc = product_desc.strip()
    if not desc:
        errors.append("商品需求/名称不能为空")

    try:
        qty = int(quantity)
        if qty <= 0:
            errors.append("数量必须大于 0")
    except (TypeError, ValueError):
        qty = 0
        errors.append("数量必须是整数")

    try:
        amount = float(sale_amount)
        if amount <= 0:
            errors.append("销售金额必须大于 0")
    except (TypeError, ValueError):
        amount = 0.0
        errors.append("销售金额必须是数字")

    return parsed_date, desc, qty, amount, errors


def _order_form_values(order: Order) -> dict:
    return {
        "platform": order.platform or "",
        "shop": order.shop or "",
        "order_date": order.order_date.isoformat(),
        "order_time": order.order_time or "",
        "product_desc": order.product_desc,
        "quantity": order.quantity,
        "sale_amount": f"{order.sale_amount:g}",
        "sale_currency": order.sale_currency,
    }


def _history_items(db: Session, user: User, limit: int = 20) -> list[dict]:
    rows = recent_products(db, user.id, limit=limit)
    return [{"desc": row.product_desc, "use_count": row.use_count} for row in rows]


def _existing_image_items(db: Session, order: Order) -> list[dict]:
    storage = get_storage()
    rows = db.scalars(
        select(OrderImage).where(OrderImage.order_id == order.id).order_by(OrderImage.id)
    ).all()
    return [
        {
            "key": row.cos_key,
            "filename": row.filename or "截图",
            "preview": storage.url_for(row.cos_key),
        }
        for row in rows
    ]


def _render_order_form(
    request: Request,
    user: User,
    db: Session,
    *,
    form: dict,
    errors: list[str],
    order: Order | None = None,
    status_code: int = 200,
):
    context = {
        "user": user,
        "history_items": _history_items(db, user),
        "today": date.today().isoformat(),
        "form": form,
        "errors": errors,
        "form_action": f"/orders/{order.id}/edit" if order else "/orders/new",
        "edit_order": order,
        "existing_images": _existing_image_items(db, order) if order else [],
    }
    return templates.TemplateResponse(
        request, "order_new.html", context, status_code=status_code
    )


# --------------------------------------------------------------------------- #
# 接口：商品需求快速复用
# --------------------------------------------------------------------------- #
@api_router.get("/product-history")
def product_history(
    q: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows = recent_products(db, user.id, limit=20, keyword=q.strip() or None)
    return {"items": [{"desc": r.product_desc, "use_count": r.use_count} for r in rows]}


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
@router.get("")
def order_list(
    request: Request,
    status: str = Query(""),
    q: str = Query(""),
    owner: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    page: int = Query(1, ge=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    keyword = q.strip()
    # 上海仓平时只看待处理的单，但只要用了搜索，就允许查全库（文档：按单号/快递单查询）
    warehouse_search = bool(keyword) and user.role == Role.WAREHOUSE.value

    conditions = []
    visibility = visibility_filter(user, warehouse_search=warehouse_search)
    if visibility is not None:
        conditions.append(visibility)

    if status and status in Status.values():
        conditions.append(Order.status == status)

    if keyword:
        like = f"%{keyword}%"
        tracking_match = exists().where(
            (Purchase.order_id == Order.id) & (Purchase.domestic_tracking_no.like(like))
        )
        conditions.append(
            or_(Order.order_no.like(like), Order.product_desc.like(like), tracking_match)
        )

    if owner and user.role == Role.ADMIN.value and owner.isdigit():
        conditions.append(Order.salesperson_id == int(owner))

    start, end = _parse_date(date_from), _parse_date(date_to)
    if start:
        conditions.append(Order.order_date >= start)
    if end:
        conditions.append(Order.order_date <= end)

    count_stmt = select(func.count()).select_from(Order)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
    total = int(db.scalar(count_stmt) or 0)

    stmt = select(Order).options(joinedload(Order.salesperson))
    for condition in conditions:
        stmt = stmt.where(condition)
    rows = (
        db.scalars(
            stmt.order_by(Order.order_date.desc(), Order.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        .unique()
        .all()
    )

    salespeople = (
        db.scalars(select(User).where(User.role == Role.SALES.value).order_by(User.id)).all()
        if user.role == Role.ADMIN.value
        else []
    )

    return templates.TemplateResponse(
        request,
        "order_list.html",
        {
            "user": user,
            "orders": rows,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "statuses": MAIN_FLOW,
            "salespeople": salespeople,
            "can_create": can_create_order(user),
            "filters": {
                "status": status,
                "q": q,
                "owner": owner,
                "date_from": date_from,
                "date_to": date_to,
            },
        },
    )


# --------------------------------------------------------------------------- #
# 录单
# --------------------------------------------------------------------------- #
@router.get("/new")
def order_new(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not can_create_order(user):
        raise Forbidden("只有销售和管理员可以录入订单")
    return _render_order_form(request, user, db, form={}, errors=[])


@router.post("/new")
def order_create(
    request: Request,
    platform: str = Form(""),
    shop: str = Form(""),
    order_date: str = Form(...),
    order_time: str = Form(""),
    product_desc: str = Form(...),
    quantity: str = Form(...),
    sale_amount: str = Form(...),
    sale_currency: str = Form("JPY"),
    image_keys: str = Form("[]"),
    image_names: str = Form("[]"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not can_create_order(user):
        raise Forbidden("只有销售和管理员可以录入订单")

    form = {
        "platform": platform,
        "shop": shop,
        "order_date": order_date,
        "order_time": order_time,
        "product_desc": product_desc,
        "quantity": quantity,
        "sale_amount": sale_amount,
        "sale_currency": sale_currency,
    }
    parsed_date, desc, qty, amount, errors = _validate_order_form(
        order_date, product_desc, quantity, sale_amount
    )
    if errors:
        return _render_order_form(request, user, db, form=form, errors=errors, status_code=400)

    order = create_order(
        db,
        salesperson=user,
        order_date=parsed_date,
        product_desc=desc,
        quantity=qty,
        sale_amount=amount,
        platform=platform,
        shop=shop,
        order_time=order_time,
        sale_currency=sale_currency,
        image_keys=_parse_json_list(image_keys),
        image_names=_parse_json_list(image_names),
    )
    return RedirectResponse(f"/orders/{order.id}?msg=订单已创建", status_code=303)


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
@router.get("/{order_id}")
def order_detail(
    order_id: int,
    request: Request,
    msg: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    order = db.get(Order, order_id)
    if order is None:
        raise Forbidden("订单不存在或你没有权限查看")
    if not can_view_order(db, user, order):
        raise Forbidden("你没有权限查看这笔订单")

    images = db.scalars(
        select(OrderImage).where(OrderImage.order_id == order.id).order_by(OrderImage.id)
    ).all()
    logs = db.scalars(
        select(StatusLog).where(StatusLog.order_id == order.id).order_by(StatusLog.id.desc())
    ).all()

    operators = {
        u.id: u.display_name
        for u in db.scalars(
            select(User).where(User.id.in_([log.operator_id for log in logs if log.operator_id]))
        ).all()
    }

    storage = get_storage()
    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "user": user,
            "order": order,
            "salesperson": db.get(User, order.salesperson_id) if order.salesperson_id else None,
            "images": images,
            "image_urls": {img.id: storage.url_for(img.cos_key) for img in images},
            "logs": logs,
            "operators": operators,
            "msg": msg,
            "can_edit": can_edit_order(user, order),
        },
    )


# --------------------------------------------------------------------------- #
# 编辑（限未完成订单；销售只能改自己的）
# --------------------------------------------------------------------------- #
@router.get("/{order_id}/edit")
def order_edit_form(
    order_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    order = db.get(Order, order_id)
    if order is None:
        raise Forbidden("订单不存在")
    if not can_edit_order(user, order):
        raise Forbidden("该订单不可编辑（已完成/已取消，或不是你的单）")

    return _render_order_form(
        request, user, db, form=_order_form_values(order), errors=[], order=order
    )


@router.post("/{order_id}/edit")
def order_edit_submit(
    order_id: int,
    request: Request,
    platform: str = Form(""),
    shop: str = Form(""),
    order_date: str = Form(...),
    order_time: str = Form(""),
    product_desc: str = Form(...),
    quantity: str = Form(...),
    sale_amount: str = Form(...),
    sale_currency: str = Form("JPY"),
    image_keys: str = Form("[]"),
    image_names: str = Form("[]"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    order = db.get(Order, order_id)
    if order is None:
        raise Forbidden("订单不存在")
    if not can_edit_order(user, order):
        raise Forbidden("该订单不可编辑（已完成/已取消，或不是你的单）")

    parsed_date, desc, qty, amount, errors = _validate_order_form(
        order_date, product_desc, quantity, sale_amount
    )
    if errors:
        form = {
            "platform": platform,
            "shop": shop,
            "order_date": order_date,
            "order_time": order_time,
            "product_desc": product_desc,
            "quantity": quantity,
            "sale_amount": sale_amount,
            "sale_currency": sale_currency,
        }
        return _render_order_form(
            request, user, db, form=form, errors=errors, order=order, status_code=400
        )

    changed: list[str] = []
    if order.product_desc != desc:
        changed.append("商品需求")
    if order.quantity != qty:
        changed.append("数量")
    if float(order.sale_amount) != amount:
        changed.append("销售金额")
    if (order.platform or "") != platform.strip():
        changed.append("平台")
    if (order.shop or "") != shop.strip():
        changed.append("店铺")
    if (order.order_time or "") != order_time.strip():
        changed.append("下单时间")
    if order.order_date != parsed_date:
        changed.append("下单日期")

    order.platform = platform.strip() or None
    order.shop = shop.strip() or None
    order.order_date = parsed_date
    order.order_time = order_time.strip() or None
    order.product_desc = desc
    order.quantity = qty
    order.sale_amount = amount
    order.sale_currency = sale_currency or "JPY"

    # 截图：保留仍在列表里的，删掉被移除的，补上新增的
    keep_keys = _parse_json_list(image_keys)
    names = _parse_json_list(image_names)
    existing = db.scalars(select(OrderImage).where(OrderImage.order_id == order.id)).all()
    known = {row.cos_key for row in existing}
    for row in existing:
        if row.cos_key not in keep_keys:
            db.delete(row)
    for index, key in enumerate(keep_keys):
        if key not in known:
            db.add(
                OrderImage(
                    order_id=order.id,
                    cos_key=key,
                    filename=names[index] if index < len(names) else None,
                    uploaded_by=user.id,
                )
            )

    db.add(
        StatusLog(
            order_id=order.id,
            from_status=order.status,
            to_status=order.status,
            operator_id=user.id,
            note="修改订单信息" + ("：" + "、".join(changed) if changed else "（无实质变更）"),
        )
    )
    db.commit()
    return RedirectResponse(f"/orders/{order.id}?msg=订单已更新", status_code=303)
