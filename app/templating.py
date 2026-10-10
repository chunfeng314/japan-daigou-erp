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
from .timeutil import format_display

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

#: 模板里统一用 `{{ x.created_at | dt }}` —— 别直接 strftime，库里是 UTC 会少 8 小时
templates.env.filters["dt"] = format_display


def _money_filter(value, currency=None):
    """`{{ item | money }}`（MoneyItem）或 `{{ total | money(currency) }}`（数字）。"""
    from .money import MoneyItem, format_money

    if isinstance(value, MoneyItem):
        return format_money(value.amount, value.currency)
    return format_money(value, currency)


def _currency_name_filter(value):
    from .money import currency_label

    return currency_label(value)


templates.env.filters["money"] = _money_filter
templates.env.filters["currency_name"] = _currency_name_filter

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
