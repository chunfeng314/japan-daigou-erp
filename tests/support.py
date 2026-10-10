"""测试公共工具。

`wipe_all()` 按**子表 → 父表**的顺序清库。手写删除顺序迟早会漏表
（第一版就漏了 order_images / product_history，导致 `DELETE FROM users` 外键失败），
所以统一收在这里，顺序只维护一份。
"""

from __future__ import annotations

import atexit

from sqlalchemy import delete

from app.db import get_sessionmaker, reset_state
from app.models import (
    JpDelivery,
    JpShipment,
    Order,
    OrderException,
    OrderImage,
    ProductHistory,
    Purchase,
    Settlement,
    ShReceive,
    StatusLog,
    User,
)

#: 删除顺序：被引用的表排在后面
DELETE_ORDER = (
    StatusLog,
    OrderImage,
    OrderException,
    Settlement,
    JpDelivery,
    JpShipment,
    ShReceive,
    Purchase,
    ProductHistory,
    Order,
    User,
)


def wipe_all() -> None:
    session = get_sessionmaker()()
    try:
        for model in DELETE_ORDER:
            session.execute(delete(model))
        session.commit()
    finally:
        session.close()


# 进程退出时把引擎回收掉，否则 CPython 的 sqlite3 会报
# "ResourceWarning: unclosed database"（只是测试输出噪音，生产无影响）
atexit.register(reset_state)
