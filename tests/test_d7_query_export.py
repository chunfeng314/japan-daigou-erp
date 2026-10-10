"""D7：卡单检测 / 数据导出 / 权限收口。

权限收口用「遍历所有路由」的方式复查 —— 逐个手写容易漏，路由一多就会忘。
"""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-d7-test-"))
config.DB_PATH = _TMP_DIR / "d7.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"

reset_state()
init_db()

from app import sla  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, StatusLog, User  # noqa: E402
from app.order_service import create_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.sla import overdue_count, status_since, stay_info, utcnow_naive  # noqa: E402
from app.state_machine import MAIN_FLOW, Role, Status  # noqa: E402
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


def _session():
    return get_sessionmaker()()


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        wipe_all()
        self.admin_id = _create_user("admin", Role.ADMIN.value, "管理员")
        self.sales_id = _create_user("sales", Role.SALES.value, "销售甲")
        self.sales2_id = _create_user("sales2", Role.SALES.value, "销售乙")
        self.buyer_id = _create_user("buyer", Role.PURCHASER.value, "采购")
        self.wh_id = _create_user("wh", Role.WAREHOUSE.value, "仓管")
        self.client.cookies.clear()

    def login(self, username: str) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)

    def make_order(self, *, sales_id: int | None = None, status: Status | None = None,
                   days_ago: int = 0, **kwargs) -> Order:
        session = _session()
        try:
            owner = session.get(User, sales_id or self.sales_id)
            order = create_order(
                session,
                salesperson=owner,
                order_date=date(2026, 10, 10),
                product_desc=kwargs.pop("product_desc", "限定 香水 100ml"),
                quantity=kwargs.pop("quantity", 1),
                sale_amount=kwargs.pop("sale_amount", 4200.0),
                **kwargs,
            )
            if status is not None:
                order.status = status.value
            # 把「进入当前状态」的时间往前拨，模拟卡单
            log = session.scalars(
                select(StatusLog).where(StatusLog.order_id == order.id).order_by(StatusLog.id)
            ).all()[-1]
            # ⚠️ 库里存的是 **naive UTC**，别用 datetime.now()（那是本地时间，会差 8 小时）
            log.created_at = utcnow_naive() - timedelta(days=days_ago)
            session.commit()
            session.refresh(order)
            return order
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestSlaBasics(BaseCase):
    def test_defaults_cover_every_open_status(self):
        for status in MAIN_FLOW[:-1]:  # 最后一个是「已完成」
            self.assertIsNotNone(sla.sla_days(status), f"{status.value} 缺 SLA 阈值")
        self.assertIsNone(sla.sla_days(Status.COMPLETED))
        self.assertIsNone(sla.sla_days(Status.CANCELLED))

    def test_env_override_parsing(self):
        import os

        original = os.environ.get("ERP_SLA_DAYS")
        os.environ["ERP_SLA_DAYS"] = "待采购=2,采购中=5,不存在的状态=9"
        try:
            table = sla._load_sla()
            self.assertEqual(table[Status.PENDING_PURCHASE], 2)
            self.assertEqual(table[Status.PURCHASING], 5)
            # 认不出的名字被忽略，其余仍是默认值
            self.assertEqual(table[Status.WAITING_SH_RECEIPT], sla.DEFAULT_SLA_DAYS[Status.WAITING_SH_RECEIPT])
        finally:
            if original is None:
                os.environ.pop("ERP_SLA_DAYS", None)
            else:
                os.environ["ERP_SLA_DAYS"] = original

    def test_stay_info_not_overdue(self):
        now = datetime(2026, 10, 10, 12, 0)
        info = stay_info(Status.PURCHASING, now - timedelta(days=1), now=now)
        self.assertEqual(info.days, 1)
        self.assertEqual(info.limit, 3)
        self.assertFalse(info.overdue)

    def test_stay_info_overdue(self):
        now = datetime(2026, 10, 10, 12, 0)
        info = stay_info(Status.PURCHASING, now - timedelta(days=4), now=now)
        self.assertTrue(info.overdue)

    def test_terminal_status_never_overdue(self):
        now = datetime(2026, 10, 10, 12, 0)
        info = stay_info(Status.COMPLETED, now - timedelta(days=99), now=now)
        self.assertFalse(info.overdue)
        self.assertIsNone(info.limit)

    def test_status_since_uses_latest_log(self):
        order = self.make_order()
        session = _session()
        try:
            # 再补一条流转记录，应该取最后那条
            session.add(StatusLog(order_id=order.id, to_status=Status.PURCHASING.value, note="后加的"))
            session.commit()
            since = status_since(session, [order.id])[order.id]
            self.assertAlmostEqual(
                (utcnow_naive() - since).total_seconds(), 0, delta=10
            )
        finally:
            session.close()

    def test_status_since_batch_and_empty(self):
        session = _session()
        try:
            self.assertEqual(status_since(session, []), {})
        finally:
            session.close()

    def test_overdue_count(self):
        self.make_order(status=Status.PURCHASING, days_ago=5)     # 超（限 3 天）
        self.make_order(status=Status.PURCHASING, days_ago=1)     # 不超
        self.make_order(status=Status.COMPLETED, days_ago=99)     # 终态不算
        session = _session()
        try:
            self.assertEqual(overdue_count(session), 1)
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestOverdueInUi(BaseCase):
    def test_list_marks_overdue(self):
        self.make_order(status=Status.PURCHASING, days_ago=10)
        self.login("admin")
        page = self.client.get("/orders").text
        self.assertIn("超时", page)
        self.assertIn("stay-over", page)

    def test_list_shows_stay_column(self):
        self.make_order(status=Status.PURCHASING, days_ago=1)
        self.login("admin")
        self.assertIn("限 3 天", self.client.get("/orders").text)

    def test_detail_shows_stay(self):
        order = self.make_order(status=Status.PURCHASING, days_ago=10)
        self.login("admin")
        detail = self.client.get(f"/orders/{order.id}").text
        self.assertIn("当前状态停留", detail)
        self.assertIn("超时", detail)

    def test_home_shows_overdue_card_for_every_role(self):
        self.make_order(status=Status.PURCHASING, days_ago=10)
        for username in ("admin", "sales", "buyer", "wh"):
            self.client.cookies.clear()
            self.login(username)
            self.assertIn("卡单提醒", self.client.get("/").text, f"{username} 首页缺卡单提醒")

    def test_home_overdue_respects_visibility(self):
        """销售乙看不到销售甲的卡单。"""
        self.make_order(sales_id=self.sales_id, status=Status.PURCHASING, days_ago=10)
        self.login("sales2")
        page = self.client.get("/").text
        self.assertIn("卡单提醒", page)
        # 该卡片计数应为 0（渲染成 >0< 的只有非零）
        self.assertIn('<span class="todo-count">0</span>', page)


# --------------------------------------------------------------------------- #
class TestExport(BaseCase):
    def _csv_rows(self, response) -> list[list[str]]:
        text = response.content.decode("utf-8").lstrip("\ufeff")
        return list(csv.reader(io.StringIO(text)))

    def test_admin_exports_all(self):
        self.make_order(product_desc="甲的单")
        self.make_order(sales_id=self.sales2_id, product_desc="乙的单")
        self.login("admin")
        response = self.client.get("/orders/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])

        rows = self._csv_rows(response)
        self.assertEqual(rows[0][0], "订单号")
        self.assertIn("当前状态停留天数", rows[0])
        self.assertEqual(len(rows), 3)  # 表头 + 2 行
        joined = "\n".join(",".join(r) for r in rows)
        self.assertIn("甲的单", joined)
        self.assertIn("乙的单", joined)

    def test_sales_exports_own_only(self):
        self.make_order(product_desc="甲的单")
        self.make_order(sales_id=self.sales2_id, product_desc="乙的单")
        self.login("sales")
        rows = self._csv_rows(self.client.get("/orders/export.csv"))
        joined = "\n".join(",".join(r) for r in rows)
        self.assertIn("甲的单", joined)
        self.assertNotIn("乙的单", joined)

    def test_purchaser_and_warehouse_are_forbidden(self):
        for username in ("buyer", "wh"):
            self.client.cookies.clear()
            self.login(username)
            self.assertEqual(
                self.client.get("/orders/export.csv").status_code, 403, f"{username} 不该能导出"
            )

    def test_anonymous_redirected(self):
        response = self.client.get("/orders/export.csv", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_export_respects_status_filter(self):
        self.make_order(status=Status.PENDING_PURCHASE, product_desc="待采购的单")
        self.make_order(status=Status.COMPLETED, product_desc="完成的单")
        self.login("admin")
        rows = self._csv_rows(self.client.get("/orders/export.csv?status=已完成"))
        self.assertEqual(len(rows), 2)
        self.assertIn("完成的单", ",".join(rows[1]))

    def test_export_has_bom_for_excel(self):
        self.make_order()
        self.login("admin")
        self.assertTrue(self.client.get("/orders/export.csv").content.startswith(b"\xef\xbb\xbf"))

    def test_export_not_shadowed_by_order_id_route(self):
        """/orders/export.csv 必须注册在 /orders/{order_id} 之前，否则会被当成订单 id。"""
        self.login("admin")
        response = self.client.get("/orders/export.csv")
        self.assertNotEqual(response.status_code, 422)


# --------------------------------------------------------------------------- #
class TestRouteAuthorizationSweep(BaseCase):
    """权限收口复查：遍历所有路由，确认匿名访问一律进不去。

    手写清单迟早会漏，所以从 `app.openapi()` 取全部 path+method。
    （不用 `app.routes` —— 新版 FastAPI 把 include_router 变成了嵌套的
    `_IncludedRouter`，`app.routes` 不再摊平子路由，遍历不到东西。）
    """

    PUBLIC = {
        "/login",
        "/logout",
        "/healthz",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }

    #: 必须被覆盖到的关键路径，否则说明遍历方式又失效了
    REQUIRED_PATHS = {
        "/",
        "/orders",
        "/orders/new",
        "/orders/export.csv",
        "/orders/{order_id}",
        "/orders/{order_id}/edit",
        "/orders/{order_id}/transition",
        "/orders/{order_id}/exceptions",
        "/admin/users",
        "/api/product-history",
        "/api/uploads",
        "/api/uploads/sign",
        "/files/{key}",
    }

    def _fill_path(self, path: str) -> str:
        return (
            path.replace("{order_id}", "1")
            .replace("{exception_id}", "1")
            .replace("{user_id}", "1")
            .replace("{key}", "x.png")
        )

    def _iter_paths(self) -> list[tuple[str, str]]:
        schema = app.openapi()
        pairs: list[tuple[str, str]] = []
        for path, operations in schema["paths"].items():
            for method in operations:
                if method.upper() in {"HEAD", "OPTIONS"}:
                    continue
                pairs.append((method.upper(), path))
        return pairs

    def test_sweep_covers_expected_paths(self):
        found = {path for _, path in self._iter_paths()}
        missing = self.REQUIRED_PATHS - found
        self.assertEqual(missing, set(), f"这些路径没被遍历到：{missing}")

    def test_every_protected_route_rejects_anonymous(self):
        checked = 0
        for method, path in self._iter_paths():
            if path in self.PUBLIC:
                continue
            url = self._fill_path(path)
            self.client.cookies.clear()
            response = self.client.request(method, url, follow_redirects=False)
            self.assertIn(
                response.status_code,
                (303, 403),
                f"匿名 {method} {url} 返回了 {response.status_code}，应被拦下",
            )
            checked += 1
        self.assertGreater(checked, 10, "路由遍历没覆盖到东西，检查筛选条件")

    def test_healthz_is_public(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get("/login").status_code, 200)


if __name__ == "__main__":
    unittest.main()
