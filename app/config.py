"""集中配置：全部可通过环境变量覆盖，方便本地 / 测试 / 东京部署三套环境。"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """读取项目根目录的 .env（存在才读，零依赖实现）。"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _db_path() -> Path:
    raw = os.environ.get("ERP_DB_PATH", "data/erp.sqlite3")
    path = Path(raw)
    return path if path.is_absolute() else (BASE_DIR / path)


DB_PATH: Path = _db_path()
SECRET_KEY: str = os.environ.get("ERP_SECRET_KEY", "dev-insecure-secret-change-me")
SESSION_COOKIE: str = os.environ.get("ERP_SESSION_COOKIE", "erp_session")
SESSION_MAX_AGE: int = int(os.environ.get("ERP_SESSION_MAX_AGE", 60 * 60 * 12))

#: 部署到 HTTPS 后置 1，让会话 cookie 带 Secure（本地 http 调试保持在 0）
COOKIE_SECURE: bool = os.environ.get("ERP_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}

ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_DISPLAY_NAME: str = os.environ.get("ADMIN_DISPLAY_NAME", "系统管理员")

APP_TITLE = "日代 ERP"
