"""登录 / 登出。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..security import create_session_token, verify_password
from ..templating import templates

router = APIRouter(tags=["auth"])


def _safe_next(target: str | None) -> str:
    """只允许站内相对路径，挡掉开放重定向（`//evil.com`、`http://...`）。"""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login")
def login_page(
    request: Request,
    next: str = "/",
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    if user is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "error": None}
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    account = db.scalar(select(User).where(User.username == username.strip()))
    ok = (
        account is not None
        and bool(account.is_active)
        and verify_password(password, account.password_hash)
    )
    if not ok:
        # 统一文案：不区分"账号不存在 / 密码错 / 已停用"，避免账号枚举
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "error": "账号或密码不正确（账号被停用也会看到这条）"},
            status_code=400,
        )

    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        config.SESSION_COOKIE,
        create_session_token(account.id),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return response
