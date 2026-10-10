"""Jinja2 模板环境（集中在这里，避免各路由重复构造）。"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from . import config
from .state_machine import (  # noqa: F401  供模板使用
    FIELD_LABELS,
    MAIN_FLOW,
    ROLE_LABELS,
    STATUS_TONE_BY_VALUE,
    ExceptionStatus,
    ExceptionType,
    Role,
    Status,
)

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

templates.env.globals.update(
    app_title=config.APP_TITLE,
    app_version=__import__("app").__version__,
    Role=Role,
    Status=Status,
    ROLE_LABELS=ROLE_LABELS,
    MAIN_FLOW=MAIN_FLOW,
    FIELD_LABELS=FIELD_LABELS,
    STATUS_TONES=STATUS_TONE_BY_VALUE,
    ExceptionType=ExceptionType,
    ExceptionStatus=ExceptionStatus,
)
