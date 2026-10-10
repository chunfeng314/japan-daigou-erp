"""账号管理（仅管理员）：新增 / 停用 / 启用 / 重置密码。

停用 = 不可登录但数据保留（见文档 §4）。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_role
from ..models import User
from ..security import hash_password
from ..state_machine import ROLE_LABELS, Role
from ..templating import templates

router = APIRouter(prefix="/admin/users", tags=["users"])

MIN_PASSWORD_LENGTH = 6


def _back(msg: str | None = None, error: str | None = None) -> RedirectResponse:
    query = []
    if msg:
        query.append(f"msg={quote(msg)}")
    if error:
        query.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return RedirectResponse(f"/admin/users{suffix}", status_code=303)


@router.get("")
def user_list(
    request: Request,
    msg: str | None = None,
    error: str | None = None,
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    users = db.scalars(select(User).order_by(User.is_active.desc(), User.id)).all()
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "user": user,
            "users": users,
            "roles": [(r.value, ROLE_LABELS[r]) for r in Role],
            "msg": msg,
            "error": error,
        },
    )


@router.post("")
def create_user(
    username: str = Form(...),
    display_name: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    username = username.strip()
    display_name = display_name.strip()

    if not username or not display_name:
        return _back(error="账号和姓名都不能为空")
    if role not in {r.value for r in Role}:
        return _back(error="角色不合法")
    if len(password) < MIN_PASSWORD_LENGTH:
        return _back(error=f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    if db.scalar(select(User).where(User.username == username)) is not None:
        return _back(error=f"账号「{username}」已存在")

    db.add(
        User(
            username=username,
            display_name=display_name,
            role=role,
            password_hash=hash_password(password),
            is_active=1,
        )
    )
    db.commit()
    return _back(msg=f"已新增账号「{display_name}」（{username}）")


@router.post("/{user_id}/toggle")
def toggle_user(
    user_id: int,
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return _back(error="账号不存在")
    if target.id == user.id:
        return _back(error="不能停用/启用自己")

    target.is_active = 0 if target.is_active else 1
    db.commit()
    action = "启用" if target.is_active else "停用"
    return _back(msg=f"已{action}账号「{target.display_name}」")


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    new_password: str = Form(...),
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return _back(error="账号不存在")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return _back(error=f"密码至少 {MIN_PASSWORD_LENGTH} 位")

    target.password_hash = hash_password(new_password)
    db.commit()
    return _back(msg=f"已重置「{target.display_name}」的密码")
