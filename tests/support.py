"""测试公共工具。

`wipe_all()` 按**子表 → 父表**的顺序清库。手写删除顺序迟早会漏表
（第一版就漏了 order_images / product_history，导致 `DELETE FROM users` 外键失败），
所以统一收在这里，顺序只维护一份。
"""

from __future__ import annotations

import atexit
import struct
import zlib

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


# --------------------------------------------------------------------------- #
def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def tiny_png(width: int = 1, height: int = 1, rgb: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    """生成一个**合法的**最小 PNG（纯 stdlib，无第三方依赖）。

    ⚠️ 别再用网上抄来的 base64 常量当 PNG —— 有些根本不是合法 PNG，
    本地存盘不解析所以看不出来，一旦送去给模型就会被拒
    （报 `unsupported image`）。这个坑真踩过一次。
    """
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width  # 每行前面一个 filter 字节
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit / truecolor RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
