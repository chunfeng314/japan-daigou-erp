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

# --------------------------------------------------------------------------- #
# 文件存储（订单截图）
#   local = 存本机 data/uploads/，开发与单机部署用（零外部依赖）
#   cos   = 腾讯云 COS，生产用；截图由浏览器**直传**，不经服务器中转
# --------------------------------------------------------------------------- #
STORAGE_BACKEND: str = os.environ.get("ERP_STORAGE", "local").strip().lower()

UPLOAD_DIR: Path = BASE_DIR / os.environ.get("ERP_UPLOAD_DIR", "data/uploads")
MAX_UPLOAD_BYTES: int = int(os.environ.get("ERP_MAX_UPLOAD_MB", "10")) * 1024 * 1024
ALLOWED_IMAGE_EXTS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp"}
)

#: COS 预签名 URL 有效期（秒）
COS_SIGNED_URL_EXPIRE: int = int(os.environ.get("ERP_COS_SIGN_EXPIRE", "900"))
COS_SECRET_ID: str = os.environ.get("COS_SECRET_ID", "")
COS_SECRET_KEY: str = os.environ.get("COS_SECRET_KEY", "")
COS_REGION: str = os.environ.get("COS_REGION", "")
COS_BUCKET: str = os.environ.get("COS_BUCKET", "")
COS_PREFIX: str = os.environ.get("COS_PREFIX", "order-images").strip("/")

APP_TITLE = "日代 ERP"
