"""初始化数据库：建表 + 首次创建管理员账号（幂等，可重复执行）。

用法（项目根目录）：
    .venv/bin/python -m scripts.init_db

口令来源优先级：环境变量 ADMIN_PASSWORD > .env 里的 ADMIN_PASSWORD > 随机生成并打印。
**没配就随机生成**，避免出现 admin/admin 这种一眼能猜到的弱口令。
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

if __package__ in (None, ""):  # 支持 `python scripts/init_db.py` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import config  # noqa: E402
from app.db import get_sessionmaker, init_db  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role  # noqa: E402


def main() -> int:
    init_db()
    print(f"数据库位置：{config.DB_PATH}")

    session = get_sessionmaker()()
    try:
        existing_admin = session.scalar(select(User).where(User.role == Role.ADMIN.value))
        if existing_admin is not None:
            print(f"管理员已存在：{existing_admin.username}（{existing_admin.display_name}），跳过创建。")
            return 0

        username = config.ADMIN_USERNAME
        if session.scalar(select(User).where(User.username == username)) is not None:
            print(f"账号「{username}」已被非管理员占用，请用 ADMIN_USERNAME 换一个名字。", file=sys.stderr)
            return 1

        generated = not config.ADMIN_PASSWORD
        password = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)
        session.add(
            User(
                username=username,
                display_name=config.ADMIN_DISPLAY_NAME,
                role=Role.ADMIN.value,
                password_hash=hash_password(password),
                is_active=1,
            )
        )
        session.commit()

        print("已创建管理员账号：")
        print(f"  账号：{username}")
        print(f"  密码：{password}" + ("    ← 本次随机生成，请立即保存" if generated else ""))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
