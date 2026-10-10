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


def init_db() -> None:
    """建表（幂等）。"""
    from . import models  # noqa: F401  确保所有表已注册到 Base.metadata

    Base.metadata.create_all(get_engine())


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
