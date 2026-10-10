"""订单：列表 / 录单 / 详情 / 编辑。

D2~3 范围：销售录入与查看。状态推进（采购/收货/带货/发货/结算/异常）在 D4 之后逐步上线。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, joinedload

from .. import config
from ..agent.extract import extract_order_fields
from ..db import get_db
from ..deps import Forbidden, require_user
from ..models import (
    JpDelivery,
    JpShipment,
    Order,
    OrderException,
    OrderImage,
    Purchase,
    Settlement,
    StatusLog,
    User,
    ShReceive,
)
from ..order_service import create_order, recent_products
from ..permissions import (
    can_create_order,
    can_edit_order,
    can_export_orders,
    can_view_order,
    visibility_filter,
)
from ..sla import stay_info, stay_map
from ..state_machine import MAIN_FLOW, Role, Status
from ..storage import StorageError, get_storage, normalize_ext
from ..templating import templates
from ..timeutil import format_display
from ..transition_service import options_for

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


def _resolve_purchaser(db: Session, raw: str | None) -> User | None:
    """把表单里的采购员 id 变成 User。

    校验角色：不能把单指派给销售或仓管 —— 否则这张单会从所有采购的视野里消失。
    指派给已停用的人同样拒绝（留空 = 未分配，先接先得）。
    """
    if not raw or not raw.isdigit():
        return None
    user = db.get(User, int(raw))
    if user is None or user.role != Role.PURCHASER.value or not user.is_active:
        return None
    return user


def _purchasers(db: Session) -> list[User]:
    """可选的采购员（建单时指定用）。已停用的不给列 —— 指派给一个不能登录的人没有意义。"""
    return list(
        db.scalars(
            select(User)
            .where(User.role == Role.PURCHASER.value, User.is_active == 1)
            .order_by(User.display_name)
        ).all()
    )


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
        "purchasers": _purchasers(db),
        "assigned_id": int(form.get("assigned_purchaser_id") or 0)
        or (order.assigned_purchaser_id or 0 if order else 0),
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


def _build_conditions(
    user: User,
    *,
    status: str = "",
    q: str = "",
    owner: str = "",
    date_from: str = "",
    date_to: str = "",
) -> tuple[list, bool]:
    """列表与导出共用的筛选条件。返回（条件列表, 是否为"上海仓搜索全库"模式）。"""
    keyword = q.strip()
    # 上海仓平时只看待处理的单，但只要用了搜索，就允许查全库（文档：按单号/快递单查询）
    warehouse_search = bool(keyword) and user.role == Role.WAREHOUSE.value

    conditions: list = []
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

    return conditions, warehouse_search


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
    conditions, _ = _build_conditions(
        user, status=status, q=q, owner=owner, date_from=date_from, date_to=date_to
    )

    count_stmt = select(func.count()).select_from(Order)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
    total = int(db.scalar(count_stmt) or 0)

    stmt = select(Order).options(
        joinedload(Order.salesperson), joinedload(Order.assigned_purchaser)
    )
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
            "stays": stay_map(db, rows),
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "statuses": MAIN_FLOW,
            "salespeople": salespeople,
            "can_create": can_create_order(user),
            "can_export": can_export_orders(user),
            "filters_active": any([status, q, owner, date_from, date_to]),
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
# 导出（必须注册在 /{order_id} 之前，否则 "export.csv" 会被当成订单 id）
# --------------------------------------------------------------------------- #
@router.get("/export.csv")
def export_orders(
    status: str = Query(""),
    q: str = Query(""),
    owner: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not can_export_orders(user):
        raise Forbidden("当前角色没有导出权限（文档 §2.3：销售导自己的、管理员导全部）")

    conditions, _ = _build_conditions(
        user, status=status, q=q, owner=owner, date_from=date_from, date_to=date_to
    )
    stmt = select(Order).options(
        joinedload(Order.salesperson), joinedload(Order.assigned_purchaser)
    )
    for condition in conditions:
        stmt = stmt.where(condition)
    rows = db.scalars(stmt.order_by(Order.order_date.desc(), Order.id.desc())).unique().all()

    stays = stay_map(db, rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "订单号", "状态", "平台", "店铺", "下单日期", "下单时间", "商品需求", "数量",
            "销售金额", "币种", "销售", "采购负责人", "创建时间",
            "当前状态停留天数", "是否超时", "有无未了结异常", "取消原因",
        ]
    )
    for order in rows:
        stay = stays.get(order.id)
        writer.writerow(
            [
                order.order_no,
                order.status,
                order.platform or "",
                order.shop or "",
                order.order_date.isoformat(),
                order.order_time or "",
                order.product_desc,
                order.quantity,
                f"{order.sale_amount:g}",
                order.sale_currency,
                order.salesperson.display_name if order.salesperson else "",
                (order.assigned_purchaser.display_name if order.assigned_purchaser else ""),
                format_display(order.created_at),
                stay.days if stay and stay.days is not None else "",
                "是" if stay and stay.overdue else "",
                "是" if order.has_exception else "",
                order.cancel_reason or "",
            ]
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"orders-{stamp}.csv"
    # BOM 让 Excel 正确识别 UTF-8 中文
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# 截图识别（P3）：上传截图 → 模型提取字段 → 返回给前端**预填**
# 绝不在这里建单 —— 建单仍然走 /orders/new，照样过同一套校验。
# --------------------------------------------------------------------------- #
@router.post("/extract")
async def extract_from_screenshot(
    file: UploadFile = File(...),
    user: User = Depends(require_user),
):
    if not can_create_order(user):
        raise Forbidden("只有销售和管理员可以用截图识别")

    raw = await file.read()
    if not raw:
        return JSONResponse({"ok": False, "error": "图片是空的"}, status_code=400)
    if len(raw) > config.MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"ok": False, "error": f"图片超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"},
            status_code=400,
        )

    storage = get_storage()
    try:
        ext = normalize_ext(file.filename or ".png")
        target = storage.prepare_direct_upload(f"x{ext}")
    except StorageError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    result = extract_order_fields(raw, file.content_type or "")
    if not result.ok:
        return JSONResponse({"ok": False, "error": result.error}, status_code=400)

    # 图仍然存下来 —— 它就是这单的订单截图，不浪费用户一次选择
    storage.save_bytes(target.key, raw)
    return JSONResponse(
        {
            "ok": True,
            "key": target.key,
            "filename": file.filename or "截图",
            "fields": result.fields,
            "filled": list(result.filled),
            "error": result.error,
        }
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
    assigned_purchaser_id: str = Form(""),
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
        "assigned_purchaser_id": assigned_purchaser_id,
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
        assigned_purchaser=_resolve_purchaser(db, assigned_purchaser_id),
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
    error: str | None = None,
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
    exceptions = db.scalars(
        select(OrderException)
        .where(OrderException.order_id == order.id)
        .order_by(OrderException.id.desc())
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
            "exceptions": exceptions,
            "options": options_for(db, user, order),
            "stages": _stage_summary(db, order),
            "stay": stay_map(db, [order]).get(order.id),
            "msg": msg,
            "error": error,
            "can_edit": can_edit_order(user, order),
        },
    )


def _stage_summary(db: Session, order: Order) -> list[dict]:
    """把各环节已登记的数据摊平，便于详情页一眼看到关键信息。"""
    purchase = db.scalar(select(Purchase).where(Purchase.order_id == order.id))
    receive = db.scalar(select(ShReceive).where(ShReceive.order_id == order.id))
    shipment = db.scalar(select(JpShipment).where(JpShipment.order_id == order.id))
    delivery = db.scalar(select(JpDelivery).where(JpDelivery.order_id == order.id))
    settlement = db.scalar(select(Settlement).where(Settlement.order_id == order.id))

    blocks: list[dict] = []
    if purchase is not None and purchase.purchase_price is not None:
        blocks.append(
            {
                "title": "采购",
                "rows": [
                    ("采购成本", f"{purchase.purchase_price:,.2f} {purchase.purchase_currency}"),
                    ("来源平台", purchase.source_platform or "—"),
                    ("国内快递单号", purchase.domestic_tracking_no or "—"),
                    ("承运商", purchase.carrier or "—"),
                    ("国内运费", f"{purchase.domestic_shipping_fee:,.2f}" if purchase.domestic_shipping_fee is not None else "—"),
                    ("其他费用", f"{purchase.other_fee:,.2f}" if purchase.other_fee is not None else "—"),
                    ("来源链接", purchase.source_url or "—"),
                ],
            }
        )
    if receive is not None and (receive.received_date or receive.handler):
        blocks.append(
            {
                "title": "上海收货",
                "rows": [
                    ("是否收到", "已收到" if receive.received else "未收到"),
                    ("收货日期", receive.received_date or "—"),
                    ("经手人", receive.handler or "—"),
                    ("备注", receive.note or "—"),
                ],
            }
        )
    if shipment is not None and (shipment.carrier_person or shipment.flight_date):
        blocks.append(
            {
                "title": "带货安排",
                "rows": [
                    ("带货人", shipment.carrier_person or "—"),
                    ("联系方式", shipment.carrier_contact or "—"),
                    ("航班日期", shipment.flight_date or "—"),
                    ("航班号", shipment.flight_no or "—"),
                    ("行程", f"{shipment.from_loc or '—'} → {shipment.to_loc or '—'}"),
                    ("备注", shipment.note or "—"),
                ],
            }
        )
    if delivery is not None and delivery.ship_date:
        blocks.append(
            {
                "title": "发货给买家",
                "rows": [
                    ("发货日期", delivery.ship_date),
                    ("物流承运", delivery.carrier or "—"),
                    ("物流单号", delivery.tracking_no or "—"),
                    ("备注", delivery.note or "—"),
                ],
            }
        )
    if settlement is not None:
        blocks.append(
            {
                "title": "结算",
                "rows": [
                    ("结算日期", settlement.settle_date),
                    ("最终结算金额", f"{settlement.final_amount:,.0f} {settlement.currency}"),
                    ("平台手续费", f"{settlement.platform_fee:,.2f}" if settlement.platform_fee is not None else "—"),
                    ("日本国内运费", f"{settlement.japan_shipping:,.2f}" if settlement.japan_shipping is not None else "—"),
                    ("退款", f"{settlement.refund:,.2f}" if settlement.refund is not None else "—"),
                    ("其他扣款", f"{settlement.other_deduction:,.2f}" if settlement.other_deduction is not None else "—"),
                    ("结算单号", settlement.settle_ref or "—"),
                    ("结算期", settlement.period or "—"),
                ],
            }
        )
    return blocks


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
    assigned_purchaser_id: str = Form(""),
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
            "assigned_purchaser_id": assigned_purchaser_id,
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

    # 采购指派：只在还没人开始采购时才允许改 —— 订单已经有人在跟了，
    # 悄悄换人会让"谁负责"变得说不清。
    new_purchaser = _resolve_purchaser(db, assigned_purchaser_id)
    if order.status == Status.PENDING_PURCHASE.value and new_purchaser != order.assigned_purchaser:
        changed.append(
            "采购负责人"
            if new_purchaser is None
            else f"采购负责人→{new_purchaser.display_name}"
        )
        order.assigned_purchaser_id = new_purchaser.id if new_purchaser else None

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
