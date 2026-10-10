"""截图上传与读取。

- `POST /api/uploads`      服务端中转保存（local 模式的正常路径；cos 模式下的兜底）
- `POST /api/uploads/sign` 取直传落点（cos 模式：返回预签名 PUT，浏览器直接传 COS）
- `GET  /files/{key}`      读取截图（local 模式）；带登录校验 + 订单可见性校验
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import require_user, require_role
from ..models import Order, OrderImage, User
from ..permissions import can_view_order
from ..state_machine import Role
from ..storage import StorageError, get_storage, is_safe_key, normalize_ext

router = APIRouter(tags=["uploads"])


class SignRequest(BaseModel):
    filename: str
    content_type: str | None = None


@router.post("/api/uploads/sign")
def sign_upload(
    payload: SignRequest,
    user: User = Depends(require_role(Role.SALES, Role.PURCHASER, Role.WAREHOUSE)),
):
    """给浏览器一个直传落点。local 模式下 url 为空，前端应回退到 POST /api/uploads。"""
    try:
        target = get_storage().prepare_direct_upload(payload.filename)
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"mode": target.mode, "key": target.key, "url": target.url}


@router.post("/api/uploads")
async def upload_files(
    files: list[UploadFile] = File(...),
    user: User = Depends(require_role(Role.SALES, Role.PURCHASER, Role.WAREHOUSE)),
):
    """服务端中转保存。返回可直接写进订单的 key 列表。"""
    storage = get_storage()
    saved: list[dict] = []

    for item in files:
        raw = await item.read()
        if not raw:
            raise HTTPException(status_code=400, detail=f"{item.filename}：文件为空")
        if len(raw) > config.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"{item.filename}：超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限",
            )
        try:
            ext = normalize_ext(item.filename or "")
            target = storage.prepare_direct_upload(f"x{ext}")
            storage.save_bytes(target.key, raw)
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved.append({"key": target.key, "filename": item.filename})

    return JSONResponse({"mode": storage.mode, "files": saved})


@router.get("/files/{key:path}")
def read_file(
    key: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    storage = get_storage()
    if storage.mode != "local":
        # COS 模式下不给站内路由，直接跳预签名地址
        return RedirectResponse(storage.url_for(key), status_code=307)

    if not is_safe_key(key):
        raise HTTPException(status_code=400, detail="非法的文件键")

    linked = db.scalar(select(OrderImage).where(OrderImage.cos_key == key))
    if linked is not None:
        order = db.get(Order, linked.order_id)
        if order is None or not can_view_order(db, user, order):
            raise HTTPException(status_code=403, detail="你没有权限查看该截图")

    path = Path(config.UPLOAD_DIR) / key
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    media_type, _ = mimetypes.guess_type(key)
    return FileResponse(path, media_type=media_type or "application/octet-stream")
