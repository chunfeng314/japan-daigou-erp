"""FastAPI 依赖：当前登录人、角色守卫。

页面型应用希望"没登录 → 跳登录页"而不是抛 401，所以这里抛自定义异常，
由 main.py 注册的异常处理器转成 303 重定向 / 403 页面。
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import User
from .security import read_session_token
from .state_machine import Role


class LoginRequired(Exception):
    """未登录或会话失效。"""


class Forbidden(Exception):
    """已登录但无权限。"""

    def __init__(self, message: str = "你没有执行该操作的权限") -> None:
        super().__init__(message)
        self.message = message


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return None
    user_id = read_session_token(token)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_role(*roles: Role, allow_admin: bool = True) -> Callable[..., User]:
    """生成一个依赖：要求当前用户属于给定角色（默认管理员永远放行）。"""
    allowed = {r.value for r in roles}
    if allow_admin:
        allowed.add(Role.ADMIN.value)

    def _dependency(user: User = Depends(require_user)) -> User:
        if user.role not in allowed:
            raise Forbidden("当前角色无权访问该页面")
        return user

    return _dependency


def login_redirect_path(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return f"/login?next={quote(target, safe='')}"
