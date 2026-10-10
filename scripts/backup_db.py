"""每日备份：SQLite 全库 + 截图清单（+ 本地存储时连图片一起打包）。

用法（在项目根目录）：

    .venv/bin/python -m scripts.backup_db                 # 备份到 backups/，保留 30 天
    .venv/bin/python -m scripts.backup_db --keep-days 7
    .venv/bin/python -m scripts.backup_db --cos           # 额外上传到 COS

为什么用 `sqlite3` 的在线备份 API 而不是 `cp`：
数据库开了 WAL，直接拷 `.sqlite3` 可能拿到「主库还没 checkpoint、WAL 里才是新数据」的
半截状态 —— 那种备份平时看不出来，**恢复的时候才发现少了最近的单**。
`Connection.backup()` 是 SQLite 官方的一致性快照接口，跟 WAL 配合是安全的。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.timeutil import display_tz  # noqa: E402

BACKUP_FILE_RE = "erp-*.sqlite3"
MANIFEST_PREFIX = "manifest-"


@dataclass
class BackupResult:
    db_copy: Path
    manifest: Path
    uploads_archive: Path | None
    removed: tuple[Path, ...]


def _now() -> datetime:
    return datetime.now(display_tz())


def snapshot_database(db_path: Path, target: Path) -> None:
    """一致性快照。源库只读打开，避免影响线上写入。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            with destination:
                source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def write_manifest(db_path: Path, target: Path) -> int:
    """导出截图清单 —— 文档 §5 要求「全库备份 + 截图清单」。

    图片本体在 COS 上（或本地 uploads/），清单用来核对"该有的图在不在"。
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT o.order_no, i.cos_key, COALESCE(i.filename, '') "
            "FROM order_images i LEFT JOIN orders o ON o.id = i.order_id "
            "ORDER BY i.id"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []  # 表还不存在（比如刚建库就备份）
    finally:
        connection.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 订单截图清单（order_no\tcos_key\tfilename）"]
    lines += [f"{order_no or '-'}\t{key}\t{name}" for order_no, key, name in rows]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def archive_uploads(uploads_dir: Path, target: Path) -> Path | None:
    """本地存储模式下，图片就在磁盘上 —— 必须一起备份，否则丢了就真没了。"""
    if not uploads_dir.exists() or not any(uploads_dir.rglob("*")):
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    # 顺手滤掉 macOS 的 .DS_Store —— 备份包里不该有这种垃圾
    with tarfile.open(target, "w:gz") as tar:
        tar.add(
            uploads_dir,
            arcname="uploads",
            filter=lambda info: None if Path(info.name).name == ".DS_Store" else info,
        )
    return target


def prune(backup_dir: Path, keep_days: int, *, now: datetime | None = None) -> tuple[Path, ...]:
    """删掉 keep_days 天以前的备份文件（.sqlite3 / .tar.gz / manifest-*.txt）。"""
    now = now or _now()
    cutoff = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for path in sorted(backup_dir.iterdir()):
        if not path.is_file():
            continue
        if not (path.name.endswith(".sqlite3") or path.name.endswith(".tar.gz")
                or path.name.startswith(MANIFEST_PREFIX)):
            continue
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=display_tz())
        if stamp < cutoff:
            path.unlink()
            removed.append(path)
    return tuple(removed)


def upload_to_cos(paths: list[Path], prefix: str = "backups") -> list[str]:
    """把备份推到 COS。没装 SDK / 没配凭据就明确报错，不静默跳过。"""
    if not (config.COS_SECRET_ID and config.COS_SECRET_KEY and config.COS_BUCKET):
        raise RuntimeError("COS 未配置（需要 COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET）")
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as exc:
        raise RuntimeError("需要先装 SDK：.venv/bin/pip install cos-python-sdk-v5") from exc

    client = CosS3Client(
        CosConfig(
            Region=config.COS_REGION,
            SecretId=config.COS_SECRET_ID,
            SecretKey=config.COS_SECRET_KEY,
            Scheme="https",
        )
    )
    keys: list[str] = []
    for path in paths:
        key = f"{prefix.strip('/')}/{path.name}"
        with path.open("rb") as handle:
            client.put_object(Bucket=config.COS_BUCKET, Key=key, Body=handle)
        keys.append(key)
    return keys


def run(
    *,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    uploads_dir: Path | None = None,
    keep_days: int = 30,
    to_cos: bool = False,
) -> BackupResult:
    db_path = db_path or config.DB_PATH
    backup_dir = backup_dir or (config.BASE_DIR / "backups")
    uploads_dir = uploads_dir or config.UPLOAD_DIR

    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    stamp = _now().strftime("%Y%m%d-%H%M")
    backup_dir.mkdir(parents=True, exist_ok=True)

    db_copy = backup_dir / f"erp-{stamp}.sqlite3"
    snapshot_database(db_path, db_copy)

    manifest = backup_dir / f"{MANIFEST_PREFIX}{stamp}.txt"
    write_manifest(db_path, manifest)

    archive = None
    if config.STORAGE_BACKEND == "local":
        archive = archive_uploads(uploads_dir, backup_dir / f"uploads-{stamp}.tar.gz")

    removed = prune(backup_dir, keep_days)

    produced = [db_copy, manifest] + ([archive] if archive else [])
    if to_cos:
        made = upload_to_cos(produced)
        print(f"已上传 {len(made)} 个文件到 COS：{', '.join(made)}")

    return BackupResult(
        db_copy=db_copy, manifest=manifest, uploads_archive=archive, removed=removed
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="日代 ERP 每日备份")
    parser.add_argument("--db", type=Path, default=None, help="数据库路径（默认取配置）")
    parser.add_argument("--out", type=Path, default=None, help="备份目录（默认 <项目>/backups）")
    parser.add_argument("--keep-days", type=int, default=30, help="保留多少天，默认 30")
    parser.add_argument("--cos", action="store_true", help="额外上传到 COS")
    args = parser.parse_args()

    try:
        result = run(db_path=args.db, backup_dir=args.out, keep_days=args.keep_days, to_cos=args.cos)
    except Exception as exc:  # noqa: BLE001  备份失败要让 cron/systemd 看到非零退出码
        print(f"备份失败：{exc}", file=sys.stderr)
        return 1

    size_kb = result.db_copy.stat().st_size / 1024
    print(f"数据库备份：{result.db_copy}（{size_kb:.0f} KB）")
    print(f"截图清单：  {result.manifest}")
    if result.uploads_archive:
        print(f"图片打包：  {result.uploads_archive}")
    print(f"清理过期：  {len(result.removed)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
