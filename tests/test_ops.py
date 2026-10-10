"""运维脚本测试：备份与上线自检。

这两个脚本的价值在于「**跑之前就能发现问题**」，所以它们自己必须先被测住 ——
否则一个坏掉的备份脚本比没有备份更危险（你以为有备份）。
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-ops-test-"))
config.DB_PATH = _TMP_DIR / "ops.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "a" * 48
config.LLM_BASE_URL = ""
config.LLM_API_KEY = ""
config.LLM_MODEL = ""

reset_state()
init_db()

from app.models import Order, OrderImage, User  # noqa: E402
from app.order_service import create_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role  # noqa: E402
from app.timeutil import display_tz  # noqa: E402
from scripts import backup_db, preflight  # noqa: E402
from tests.support import tiny_png, wipe_all  # noqa: E402


def _seed(db_path: Path) -> None:
    """塞一点真实数据进去，好验证备份到底备没备到。"""
    session = get_sessionmaker()()
    try:
        user = User(
            username="sales",
            display_name="销售甲",
            role=Role.SALES.value,
            password_hash=hash_password("pw123456"),
            is_active=1,
        )
        session.add(user)
        session.commit()
        for index in range(3):
            order = create_order(
                session,
                salesperson=user,
                order_date=date(2026, 10, 10),
                product_desc=f"商品 {index}",
                quantity=1,
                sale_amount=1000.0 + index,
                image_keys=[f"2026/10/img{index}.png"],
                image_names=[f"img{index}.png"],
            )
    finally:
        session.close()


class BackupTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()

    def setUp(self) -> None:
        wipe_all()
        self.out = Path(tempfile.mkdtemp(prefix="erp-backup-out-"))
        _seed(config.DB_PATH)

    def _counts(self, path: Path) -> tuple[int, int]:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            orders = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        finally:
            connection.close()
        return users, orders

    # ---------- 备份本体 ----------
    def test_backup_is_a_readable_database_with_same_data(self):
        result = backup_db.run(backup_dir=self.out, keep_days=30)
        self.assertTrue(result.db_copy.exists())
        self.assertGreater(result.db_copy.stat().st_size, 0)
        # 不是"拷了个文件"就完事 —— 必须能打开且数据一致
        self.assertEqual(self._counts(result.db_copy), self._counts(config.DB_PATH))
        self.assertEqual(self._counts(result.db_copy), (1, 3))

    def test_manifest_lists_image_keys(self):
        result = backup_db.run(backup_dir=self.out, keep_days=30)
        text = result.manifest.read_text(encoding="utf-8")
        self.assertIn("order_no", text)          # 表头
        for index in range(3):
            self.assertIn(f"2026/10/img{index}.png", text)
        self.assertEqual(len(text.strip().splitlines()), 4)  # 表头 + 3 行

    def test_missing_database_raises(self):
        with self.assertRaises(FileNotFoundError):
            backup_db.run(db_path=self.out / "nope.sqlite3", backup_dir=self.out)

    # ---------- 轮转 ----------
    def test_prune_removes_old_keeps_recent(self):
        old = self.out / "erp-20200101-0300.sqlite3"
        recent = self.out / "erp-20990101-0300.sqlite3"
        manifest_old = self.out / "manifest-20200101-0300.txt"
        unrelated = self.out / "别删我.txt"
        for path in (old, recent, manifest_old, unrelated):
            path.write_text("x", encoding="utf-8")

        old_time = datetime.now().timestamp() - 100 * 86400
        os.utime(old, (old_time, old_time))
        os.utime(manifest_old, (old_time, old_time))

        removed = backup_db.prune(self.out, keep_days=30)

        self.assertIn(old, removed)
        self.assertIn(manifest_old, removed)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists(), "不该删新的")
        self.assertTrue(unrelated.exists(), "不该碰无关文件")

    def test_prune_keeps_everything_within_window(self):
        fresh = self.out / "erp-fresh.sqlite3"
        fresh.write_text("x", encoding="utf-8")
        self.assertEqual(backup_db.prune(self.out, keep_days=30), ())
        self.assertTrue(fresh.exists())

    # ---------- 本地存储时要连图片一起打包 ----------
    def test_local_storage_archives_uploads(self):
        original = config.STORAGE_BACKEND
        config.STORAGE_BACKEND = "local"
        try:
            uploads = Path(tempfile.mkdtemp(prefix="erp-uploads-"))
            (uploads / "2026").mkdir()
            (uploads / "2026" / "a.png").write_bytes(tiny_png(2, 2))
            result = backup_db.run(
                backup_dir=self.out, uploads_dir=uploads, keep_days=30
            )
            self.assertIsNotNone(result.uploads_archive)
            with tarfile.open(result.uploads_archive) as tar:
                names = tar.getnames()
            self.assertTrue(any(name.endswith("a.png") for name in names), names)
        finally:
            config.STORAGE_BACKEND = original

    def test_cos_mode_does_not_archive_uploads(self):
        original = config.STORAGE_BACKEND
        config.STORAGE_BACKEND = "cos"
        try:
            result = backup_db.run(backup_dir=self.out, keep_days=30)
            self.assertIsNone(result.uploads_archive, "图片在 COS 上，不该重复打包")
        finally:
            config.STORAGE_BACKEND = original

    def test_empty_uploads_dir_produces_no_archive(self):
        original = config.STORAGE_BACKEND
        config.STORAGE_BACKEND = "local"
        try:
            empty = Path(tempfile.mkdtemp(prefix="erp-empty-uploads-"))
            self.assertIsNone(backup_db.archive_uploads(empty, self.out / "x.tar.gz"))
        finally:
            config.STORAGE_BACKEND = original

    def test_cos_upload_without_credentials_fails_loudly(self):
        """没配凭据就明确报错，别静默跳过 —— 否则备份根本没上传却以为成功了。"""
        original = (config.COS_SECRET_ID, config.COS_SECRET_KEY, config.COS_BUCKET)
        config.COS_SECRET_ID = config.COS_SECRET_KEY = config.COS_BUCKET = ""
        try:
            with self.assertRaises(RuntimeError) as ctx:
                backup_db.upload_to_cos([self.out / "x.sqlite3"])
            self.assertIn("COS 未配置", str(ctx.exception))
        finally:
            config.COS_SECRET_ID, config.COS_SECRET_KEY, config.COS_BUCKET = original


# --------------------------------------------------------------------------- #
class PreflightTestCase(unittest.TestCase):
    """自检项的语义测试：该拦的必须拦，该提醒的必须提醒。"""

    def setUp(self) -> None:
        self.saved = {
            name: getattr(config, name)
            for name in (
                "SECRET_KEY",
                "COOKIE_SECURE",
                "STORAGE_BACKEND",
                "COS_SECRET_ID",
                "COS_SECRET_KEY",
                "COS_REGION",
                "COS_BUCKET",
                "LLM_BASE_URL",
                "LLM_API_KEY",
                "LLM_MODEL",
                "DISPLAY_TZ",
            )
        }
        wipe_all()

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(config, name, value)

    def _healthy(self) -> None:
        config.SECRET_KEY = "z" * 48
        config.COOKIE_SECURE = True
        config.STORAGE_BACKEND = "local"
        config.LLM_BASE_URL = config.LLM_API_KEY = ""
        config.LLM_MODEL = ""
        config.DISPLAY_TZ = "Asia/Shanghai"

    # ---------- 密钥 ----------
    def test_default_secret_is_fatal(self):
        config.SECRET_KEY = "dev-insecure-secret-change-me"
        check = preflight.check_secret_key()
        self.assertEqual(check.level, "fail")
        self.assertIn("默认值", check.title)

    def test_short_secret_is_fatal(self):
        config.SECRET_KEY = "abc"
        self.assertEqual(preflight.check_secret_key().level, "fail")

    def test_good_secret_passes(self):
        config.SECRET_KEY = "k" * 48
        self.assertEqual(preflight.check_secret_key().level, "ok")

    # ---------- cookie ----------
    def test_cookie_without_secure_warns(self):
        config.COOKIE_SECURE = False
        check = preflight.check_cookie_secure()
        self.assertEqual(check.level, "warn")
        self.assertIn("ERP_COOKIE_SECURE", check.detail)

    def test_cookie_secure_passes(self):
        config.COOKIE_SECURE = True
        self.assertEqual(preflight.check_cookie_secure().level, "ok")

    # ---------- 存储 ----------
    def test_cos_without_credentials_is_fatal(self):
        config.STORAGE_BACKEND = "cos"
        config.COS_SECRET_ID = config.COS_SECRET_KEY = ""
        config.COS_REGION = config.COS_BUCKET = ""
        check = preflight.check_storage()
        self.assertEqual(check.level, "fail")
        self.assertIn("COS_SECRET_ID", check.detail)

    def test_local_storage_warns_about_multi_machine(self):
        config.STORAGE_BACKEND = "local"
        config.UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="erp-preflight-uploads-"))
        check = preflight.check_storage()
        self.assertEqual(check.level, "warn")
        self.assertIn("多机", check.detail)

    # ---------- 模型 ----------
    def test_half_configured_model_is_fatal(self):
        config.LLM_BASE_URL = "https://api.example.com/v1"
        config.LLM_API_KEY = ""
        config.LLM_MODEL = "some-model"
        check = preflight.check_llm()
        self.assertEqual(check.level, "fail")
        self.assertIn("ERP_LLM_API_KEY", check.detail)

    def test_no_model_is_only_a_warning(self):
        config.LLM_BASE_URL = config.LLM_API_KEY = config.LLM_MODEL = ""
        check = preflight.check_llm()
        self.assertEqual(check.level, "warn")
        self.assertIn("规则模式", check.detail)

    def test_configured_model_passes(self):
        config.LLM_BASE_URL = "https://api.deepseek.com/v1"
        config.LLM_API_KEY = "sk-x"
        config.LLM_MODEL = "deepseek-flash"
        self.assertEqual(preflight.check_llm().level, "ok")

    # ---------- 时区 ----------
    def test_bad_timezone_is_fatal(self):
        from app import timeutil

        config.DISPLAY_TZ = "Not/AZone"
        timeutil.reset_tz_cache()
        try:
            self.assertEqual(preflight.check_timezone().level, "fail")
        finally:
            config.DISPLAY_TZ = "Asia/Shanghai"
            timeutil.reset_tz_cache()

    # ---------- 数据库 ----------
    def test_missing_database_warns(self):
        original = config.DB_PATH
        config.DB_PATH = Path(tempfile.mkdtemp()) / "absent.sqlite3"
        try:
            checks = preflight.check_database()
            self.assertTrue(any(c.level == "warn" and "不存在" in c.title for c in checks))
        finally:
            config.DB_PATH = original

    def test_healthy_database_checks_integrity_and_disk(self):
        """这一条只验「库本身健康」：完整性 + 磁盘空间。
        （账号为空会 fail，但那是设计如此，由 test_no_accounts_is_fatal 覆盖。）"""
        checks = {c.title: c for c in preflight.check_database()}
        self.assertEqual(checks["数据库完整性检查通过"].level, "ok")
        self.assertEqual(checks["磁盘剩余空间充足"].level, "ok")

    def test_no_accounts_is_fatal(self):
        checks = preflight.check_database()
        self.assertTrue(any(c.level == "fail" and "账号" in c.title for c in checks))

    def test_with_accounts_no_failure(self):
        session = get_sessionmaker()()
        try:
            session.add(
                User(
                    username="admin",
                    display_name="管理员",
                    role=Role.ADMIN.value,
                    password_hash=hash_password("pw123456"),
                    is_active=1,
                )
            )
            session.commit()
        finally:
            session.close()
        checks = preflight.check_database()
        self.assertEqual([c for c in checks if c.level == "fail"], [])

    # ---------- 汇总 ----------
    def test_run_all_on_healthy_config_has_no_failures(self):
        config.DB_PATH = Path(tempfile.mkdtemp()) / "healthy.sqlite3"
        config.UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="erp-preflight-up-"))
        reset_state()
        init_db()
        self._healthy()
        session = get_sessionmaker()()
        try:
            session.add(
                User(
                    username="admin",
                    display_name="管理员",
                    role=Role.ADMIN.value,
                    password_hash=hash_password("pw123456"),
                    is_active=1,
                )
            )
            session.commit()
        finally:
            session.close()

        failures = [c for c in preflight.run_all() if c.level == "fail"]
        self.assertEqual([c.title for c in failures], [])


if __name__ == "__main__":
    unittest.main()
