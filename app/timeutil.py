"""时间口径：库里一律存 **naive UTC**，展示时统一转成配置时区。

为什么要有这个模块：SQLite 不保存时区，`models.utcnow()` 写进去的是 UTC 墙上时间。
如果展示层直接 `strftime`，国内用户看到的每条时间都会**少 8 小时**
（真机上就出现过"下午 1 点创建的订单显示成 05:08"）。

所以：**任何面向用户的时间都必须过 `format_display()`**，
不要再用 `{{ x.created_at.strftime(...) }}`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

DEFAULT_FMT = "%Y-%m-%d %H:%M"

_tz_cache: ZoneInfo | None = None


def utcnow_naive() -> datetime:
    """当前 UTC 时间（naive）。库里存的就是这个口径，比较时必须用它。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def display_tz() -> ZoneInfo:
    global _tz_cache
    if _tz_cache is None:
        try:
            _tz_cache = ZoneInfo(config.DISPLAY_TZ)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            _tz_cache = ZoneInfo("UTC")
    return _tz_cache


def reset_tz_cache() -> None:
    """仅供测试：改过 config.DISPLAY_TZ 之后重建。"""
    global _tz_cache
    _tz_cache = None


def to_display(dt: datetime | None) -> datetime | None:
    """naive UTC → 配置时区的 aware datetime。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(display_tz())


def format_display(dt: datetime | None, fmt: str = DEFAULT_FMT) -> str:
    """给模板/CSV 用的格式化。None 返回「—」。"""
    local = to_display(dt)
    return local.strftime(fmt) if local is not None else "—"
