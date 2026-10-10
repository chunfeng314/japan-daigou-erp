"""时间展示口径测试。

背景：库里存 naive UTC，展示层必须转成配置时区。之前直接 `strftime`，
国内用户看到的时间**少 8 小时**（真机上「下午 1 点创建」显示成 05:08）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-time-test-"))
config.DB_PATH = _TMP_DIR / "time.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"

reset_state()
init_db()

from app import timeutil  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.order_service import create_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role  # noqa: E402
from app.timeutil import format_display, to_display, utcnow_naive  # noqa: E402
from tests.support import wipe_all  # noqa: E402

PASSWORD = "pw123456"


def _create_user(username: str, role: str, display_name: str) -> int:
    session = get_sessionmaker()()
    try:
        user = User(
            username=username,
            display_name=display_name,
            role=role,
            password_hash=hash_password(PASSWORD),
            is_active=1,
        )
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


class TestTimeutil(unittest.TestCase):
    def setUp(self) -> None:
        config.DISPLAY_TZ = "Asia/Shanghai"
        timeutil.reset_tz_cache()

    def tearDown(self) -> None:
        config.DISPLAY_TZ = "Asia/Shanghai"
        timeutil.reset_tz_cache()

    def test_naive_utc_is_treated_as_utc(self):
        """库里那种 naive 时间要被当成 UTC，而不是本地时间。"""
        converted = to_display(datetime(2026, 10, 10, 5, 8))
        self.assertIsNotNone(converted)
        self.assertEqual(converted.strftime("%Y-%m-%d %H:%M"), "2026-10-10 13:08")

    def test_format_display_shanghai(self):
        self.assertEqual(format_display(datetime(2026, 10, 10, 5, 8)), "2026-10-10 13:08")

    def test_aware_datetime_is_respected(self):
        aware = datetime(2026, 10, 10, 5, 8, tzinfo=timezone.utc)
        self.assertEqual(format_display(aware), "2026-10-10 13:08")

    def test_none_returns_dash(self):
        self.assertEqual(format_display(None), "—")
        self.assertIsNone(to_display(None))

    def test_custom_format(self):
        self.assertEqual(format_display(datetime(2026, 10, 10, 5, 8), "%H:%M"), "13:08")

    def test_timezone_is_configurable(self):
        config.DISPLAY_TZ = "Asia/Tokyo"
        timeutil.reset_tz_cache()
        self.assertEqual(format_display(datetime(2026, 10, 10, 5, 8)), "2026-10-10 14:08")

    def test_unknown_timezone_falls_back_to_utc(self):
        config.DISPLAY_TZ = "Not/AZone"
        timeutil.reset_tz_cache()
        self.assertEqual(format_display(datetime(2026, 10, 10, 5, 8)), "2026-10-10 05:08")

    def test_utcnow_naive_is_utc(self):
        delta = abs((datetime.now(timezone.utc).replace(tzinfo=None) - utcnow_naive()).total_seconds())
        self.assertLess(delta, 5)


class TestTimeInPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        wipe_all()
        self.admin_id = _create_user("admin", Role.ADMIN.value, "管理员")
        self.sales_id = _create_user("sales", Role.SALES.value, "销售甲")
        self.client.cookies.clear()
        config.DISPLAY_TZ = "Asia/Shanghai"
        timeutil.reset_tz_cache()

    def _order(self):
        session = get_sessionmaker()()
        try:
            owner = session.get(User, self.sales_id)
            return create_order(
                session,
                salesperson=owner,
                order_date=date(2026, 10, 10),
                product_desc="限定 香水",
                quantity=1,
                sale_amount=4200.0,
            )
        finally:
            session.close()

    def test_detail_page_shows_local_time_not_utc(self):
        order = self._order()
        # 造一个"UTC 与本地必然不同"的时间，避免恰好相等导致断言失效
        session = get_sessionmaker()()
        try:
            row = session.get(type(order), order.id)
            row.created_at = datetime(2026, 10, 10, 5, 8)
            session.commit()
        finally:
            session.close()

        self.client.post(
            "/login", data={"username": "admin", "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        page = self.client.get(f"/orders/{order.id}").text

        self.assertIn("2026-10-10 13:08", page, "详情页应显示东八区时间")
        self.assertNotIn("2026-10-10 05:08", page, "详情页不该原样输出 UTC")

    def test_csv_export_uses_local_time(self):
        order = self._order()
        session = get_sessionmaker()()
        try:
            row = session.get(type(order), order.id)
            row.created_at = datetime(2026, 10, 10, 5, 8)
            session.commit()
        finally:
            session.close()

        self.client.post(
            "/login", data={"username": "admin", "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        body = self.client.get("/orders/export.csv").content.decode("utf-8")
        self.assertIn("2026-10-10 13:08", body)
        self.assertNotIn("2026-10-10 05:08", body)

    def test_no_template_still_strftime_directly(self):
        """防回归：模板里不该再出现裸的 created_at.strftime（那会漏掉时区换算）。"""
        templates_dir = Path(config.BASE_DIR) / "app" / "templates"
        offenders = []
        for path in templates_dir.glob("*.html"):
            text = path.read_text(encoding="utf-8")
            if ".strftime(" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"这些模板还在直接 strftime：{offenders}")


class TestFilterPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        wipe_all()
        _create_user("admin", Role.ADMIN.value, "管理员")
        self.client.cookies.clear()
        self.client.post(
            "/login", data={"username": "admin", "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )

    def test_filter_panel_collapsed_by_default(self):
        page = self.client.get("/orders").text
        self.assertIn('<details class="panel filters">', page)
        self.assertNotIn('<details class="panel filters" open>', page)

    def test_filter_panel_open_when_filters_active(self):
        page = self.client.get("/orders?status=待采购").text
        self.assertIn('<details class="panel filters" open>', page)


if __name__ == "__main__":
    unittest.main()
