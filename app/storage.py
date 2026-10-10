"""订单截图的存储层。

为什么要抽象：文档要求生产环境「截图**直传**腾讯云 COS，不经服务器中转」，
但开发期不该被云凭据卡住。所以业务代码只认「一个 key」，由本层决定：
- `local`：存本机 `data/uploads/`，读取走受权路由 `/files/{key}`
- `cos`  ：腾讯云 COS，浏览器用预签名 URL 直传，读取走预签名 GET

⚠️ 状态：`local` 已真机验证；`cos` 已实现但**尚未用真实凭据验证过**
（还需要在 COS 控制台给存储桶配置 CORS，见 README）。
"""

from __future__ import annotations

import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config

_KEY_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_\-/.]*$")


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadTarget:
    """一次「直传」的落点信息，交给前端。"""

    mode: str
    key: str
    url: str | None = None  # cos 模式下是预签名 PUT 地址；local 模式为 None


def normalize_ext(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in config.ALLOWED_IMAGE_EXTS:
        raise StorageError(
            f"不支持的文件类型 {ext or '(无扩展名)'}，只允许："
            + "、".join(sorted(config.ALLOWED_IMAGE_EXTS))
        )
    return ext


def is_safe_key(key: str) -> bool:
    """挡掉 `..`、绝对路径等路径穿越写法。"""
    return bool(_KEY_RE.match(key)) and ".." not in key and not key.startswith("/")


def new_key(ext: str) -> str:
    now = datetime.now(timezone.utc)
    return f"{now:%Y/%m}/{now:%d}{secrets.token_hex(8)}{ext}"


class Storage(ABC):
    mode: str

    @abstractmethod
    def save_bytes(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def url_for(self, key: str) -> str:
        """返回可给浏览器用的地址（local 是站内路由，cos 是预签名 GET）。"""

    @abstractmethod
    def delete(self, key: str) -> None: ...

    def supports_direct_upload(self) -> bool:
        return False

    def presign_put(self, key: str, content_type: str | None = None) -> str:
        raise StorageError(f"{self.mode} 模式不支持浏览器直传")

    def prepare_direct_upload(self, filename: str) -> UploadTarget:
        """给前端一个落点：cos 返回预签名 PUT，local 让前端回传本服务。"""
        ext = normalize_ext(filename)
        key = new_key(ext)
        if self.supports_direct_upload():
            return UploadTarget(self.mode, key, self.presign_put(key))
        return UploadTarget(self.mode, key, None)


class LocalStorage(Storage):
    """存本机磁盘。单机部署 / 开发足够；多机部署要切 COS。"""

    mode = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or config.UPLOAD_DIR)

    def _path(self, key: str) -> Path:
        if not is_safe_key(key):
            raise StorageError("非法的存储键")
        return self.root / key

    def save_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError("文件不存在")
        return path.read_bytes()

    def url_for(self, key: str) -> str:
        return f"/files/{key}"

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()


class CosStorage(Storage):
    """腾讯云 COS。⚠️ 尚未用真实凭据验证过。

    依赖 `cos-python-sdk-v5`（延迟导入：local 模式不需要装它）。
    上线前还需要在 COS 控制台给存储桶配 CORS（允许本站 Origin 的 PUT/GET）。
    """

    mode = "cos"

    def __init__(self) -> None:
        missing = [
            name
            for name, value in {
                "COS_SECRET_ID": config.COS_SECRET_ID,
                "COS_SECRET_KEY": config.COS_SECRET_KEY,
                "COS_REGION": config.COS_REGION,
                "COS_BUCKET": config.COS_BUCKET,
            }.items()
            if not value
        ]
        if missing:
            raise StorageError("COS 配置缺失：" + "、".join(missing))

    def _client(self):
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:  # pragma: no cover
            raise StorageError(
                "COS 模式需要依赖 cos-python-sdk-v5，请先安装："
                ".venv/bin/pip install cos-python-sdk-v5"
            ) from exc
        cfg = CosConfig(
            Region=config.COS_REGION,
            SecretId=config.COS_SECRET_ID,
            SecretKey=config.COS_SECRET_KEY,
            Scheme="https",
        )
        return CosS3Client(cfg)

    def _object_key(self, key: str) -> str:
        return f"{config.COS_PREFIX}/{key}" if config.COS_PREFIX else key

    def save_bytes(self, key: str, data: bytes) -> None:
        self._client().put_object(
            Bucket=config.COS_BUCKET, Body=data, Key=self._object_key(key)
        )

    def read_bytes(self, key: str) -> bytes:
        resp = self._client().get_object(Bucket=config.COS_BUCKET, Key=self._object_key(key))
        return resp["Body"].get_raw_stream().read()

    def url_for(self, key: str) -> str:
        return self._client().get_presigned_url(
            Method="GET",
            Bucket=config.COS_BUCKET,
            Key=self._object_key(key),
            Expired=config.COS_SIGNED_URL_EXPIRE,
        )

    def presign_put(self, key: str, content_type: str | None = None) -> str:
        params = {}
        if content_type:
            params["ContentType"] = content_type
        return self._client().get_presigned_url(
            Method="PUT",
            Bucket=config.COS_BUCKET,
            Key=self._object_key(key),
            Expired=config.COS_SIGNED_URL_EXPIRE,
            Params=params or None,
        )

    def supports_direct_upload(self) -> bool:
        return True

    def delete(self, key: str) -> None:
        self._client().delete_object(Bucket=config.COS_BUCKET, Key=self._object_key(key))


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = CosStorage() if config.STORAGE_BACKEND == "cos" else LocalStorage()
    return _storage


def reset_storage() -> None:
    """仅供测试。"""
    global _storage
    _storage = None
