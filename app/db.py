"""数据库引擎与会话（SQLite + WAL）。

为什么用 SQLite：10 人 / 月 2000 单规模绰绰有余，备份 = 拷文件。
为什么用 SQLAlchemy：数据量上来后可平迁 MySQL，业务代码不用重写。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{config.DB_PATH}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        _configure_sqlite(_engine)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _sessionmaker


#: 需要给**已有库补列**的迁移：`(表名, 列名, 列定义)`。
#:
#: 为什么需要：SQLite 的 `create_all()` 只会建**缺失的表**，不会给已存在的表加列。
#: 所以新增字段只改模型是不够的 —— 老库会一直报 "no such column"。
#: 这里只做「加列」，不改类型、不删数据，保持迁移安全且幂等。
ADDITIVE_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # 采购分配：销售建单时可指定采购员；为空 = 未分配，先接先得
    ("orders", "assigned_purchaser_id", "INTEGER REFERENCES users(id)"),
)


def apply_migrations(engine=None) -> list[str]:
    """给已有库补上新增的列。返回实际做了什么（给调用方打印/测试用）。"""
    import sqlite3

    engine = engine or get_engine()
    done: list[str] = []
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        for table, column, ddl in ADDITIVE_MIGRATIONS:
            exists = cursor.execute(f"PRAGMA table_info({table})").fetchall()
            if not exists:
                continue  # 表还没建（全新库），create_all 会带上新列
            if any(row[1] == column for row in exists):
                continue
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            done.append(f"{table}.{column}")
        raw.commit()
    finally:
        raw.close()
    return done


def init_db() -> None:
    """建表 + 补列（幂等，可重复执行）。"""
    from . import models  # noqa: F401  确保所有表已注册到 Base.metadata

    engine = get_engine()
    Base.metadata.create_all(engine)
    apply_migrations(engine)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def reset_state() -> None:
    """仅供测试：丢弃已缓存的引擎与会话工厂，让下次调用按新配置重建。"""
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None
