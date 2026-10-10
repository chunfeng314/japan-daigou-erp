"""每日简报测试。

简报是**给老板看业务事实**的，所以这里重点验两件事：
1. 数字与口径跟别处一致（不然又是"两个数字打架"）；
2. 不能泄露别人范围的单（销售不该在自己的日报里看到别人的卡单）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-brief-test-"))
config.DB_PATH = _TMP_DIR / "brief.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"

reset_state()
init_db()

from app.briefing import MAX_ITEMS, build  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, StatusLog, User  # noqa: E402
from app.order_service import create_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.sla import utcnow_naive  # noqa: E402
from app.state_machine import Role, Status, next_step  # noqa: E402
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


class BriefingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        wipe_all()
        self.admin_id = _create_user("admin", Role.ADMIN.value, "管理员")
        self.sales_a = _create_user("sales_a", Role.SALES.value, "销售甲")
        self.sales_b = _create_user("sales_b", Role.SALES.value, "销售乙")
        self.buyer_id = _create_user("buyer", Role.PURCHASER.value, "采购")
        self.wh_id = _create_user("wh", Role.WAREHOUSE.value, "仓管")
        self.client.cookies.clear()

    def make_order(
        self,
        sales_id: int,
        *,
        desc: str = "限定 手办 20cm",
        status: Status | None = None,
        days_ago: int = 0,
        has_exception: int = 0,
    ) -> Order:
        session = _session()
        try:
            owner = session.get(User, sales_id)
            order = create_order(
                session,
                salesperson=owner,
                order_date=date(2026, 10, 10),
                product_desc=desc,
                quantity=1,
                sale_amount=4200.0,
            )
            if status is not None:
                order.status = status.value
            if has_exception:
                order.has_exception = 1
            log = session.scalars(
                select(StatusLog).where(StatusLog.order_id == order.id).order_by(StatusLog.id)
            ).all()[-1]
            log.created_at = utcnow_naive() - timedelta(days=days_ago)
            session.commit()
            session.refresh(order)
            return order
        finally:
            session.close()

    def build_for(self, user_id: int):
        session = _session()
        try:
            return build(session, session.get(User, user_id))
        finally:
            session.close()

    def login(self, username: str) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)


# --------------------------------------------------------------------------- #
class TestNextStepHelper(unittest.TestCase):
    def test_roles_for_each_status(self):
        nxt, roles = next_step(Status.PENDING_PURCHASE)
        self.assertEqual(nxt, Status.PURCHASING)
        self.assertIn("采购", roles)

        nxt, roles = next_step(Status.WAITING_SETTLEMENT)
        self.assertEqual(nxt, Status.COMPLETED)
        self.assertIn("销售", roles)

    def test_terminal_status_has_no_next(self):
        self.assertEqual(next_step(Status.COMPLETED), (None, ()))
        self.assertEqual(next_step(Status.CANCELLED), (None, ()))


# --------------------------------------------------------------------------- #
class TestEmptyBriefing(BriefingTestCase):
    def test_headline_when_nothing_at_all(self):
        briefing = self.build_for(self.admin_id)
        self.assertIn("没有卡单", briefing.headline)
        self.assertIn("没有未结束的订单", briefing.headline)

    def test_text_always_has_status_breakdown(self):
        briefing = self.build_for(self.admin_id)
        self.assertIn("■ 各状态堆积", briefing.text)
        self.assertIn("待采购 0", briefing.text)

    def test_scope_label(self):
        self.assertEqual(self.build_for(self.admin_id).scope, "全公司")
        self.assertIn("销售甲", self.build_for(self.sales_a).scope)


# --------------------------------------------------------------------------- #
class TestOverdueSection(BriefingTestCase):
    def test_overdue_shows_up_in_headline_and_section(self):
        order = self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=10)
        briefing = self.build_for(self.admin_id)

        self.assertIn("1 单卡住", briefing.headline)
        self.assertIn(order.order_no, briefing.headline)
        self.assertIn("采购中", briefing.headline)

        overdue_section = briefing.sections[0]
        self.assertEqual(overdue_section.title, "卡住了")
        self.assertEqual(overdue_section.total, 1)
        item = overdue_section.items[0]
        self.assertTrue(item.overdue)
        self.assertEqual(item.order_no, order.order_no)
        self.assertIn("采购", item.roles)

    def test_not_overdue_is_not_listed(self):
        self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=1)
        self.assertEqual(self.build_for(self.admin_id).sections[0].total, 0)

    def test_at_the_limit_is_not_overdue(self):
        """边界：停留正好等于上限（采购中限 3 天）**不算**超时，超过才算。"""
        at_limit = self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=3)
        over = self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=4)
        section = self.build_for(self.admin_id).sections[0]
        self.assertEqual(section.total, 1)
        self.assertEqual(section.items[0].order_no, over.order_no)
        self.assertNotEqual(section.items[0].order_no, at_limit.order_no)

    def test_worst_first_and_capped(self):
        for days in (4, 30, 12):
            self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=days)
        briefing = self.build_for(self.admin_id)
        section = briefing.sections[0]
        self.assertEqual(section.total, 3)
        self.assertEqual(section.items[0].days, 30, "最久的应该排第一")

    def test_more_than_max_items_is_noted_in_text(self):
        for _ in range(MAX_ITEMS + 3):
            self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=10)
        briefing = self.build_for(self.admin_id)
        self.assertEqual(len(briefing.sections[0].items), MAX_ITEMS)
        self.assertIn("还有 3 单", briefing.text)


# --------------------------------------------------------------------------- #
class TestRoleSections(BriefingTestCase):
    def _titles(self, user_id: int) -> list[str]:
        return [s.title for s in self.build_for(user_id).sections]

    def test_sales_sections(self):
        titles = self._titles(self.sales_a)
        self.assertIn("卡住了", titles)
        self.assertIn("待你结算", titles)
        self.assertIn("异常未了结", titles)

    def test_purchaser_sections(self):
        titles = self._titles(self.buyer_id)
        self.assertIn("待你采购", titles)
        self.assertIn("采购中", titles)

    def test_warehouse_sections(self):
        titles = self._titles(self.wh_id)
        self.assertIn("待你收货", titles)
        self.assertIn("待安排带货", titles)
        self.assertIn("待登记发货", titles)

    def test_queue_section_lists_the_orders(self):
        pending = self.make_order(self.sales_a, desc="待买的手办")
        briefing = self.build_for(self.buyer_id)
        section = next(s for s in briefing.sections if s.title == "待你采购")
        self.assertEqual(section.total, 1)
        self.assertEqual(section.items[0].order_no, pending.order_no)

    def test_exception_section(self):
        order = self.make_order(self.sales_a, has_exception=1, desc="有异常的单")
        briefing = self.build_for(self.admin_id)
        section = next(s for s in briefing.sections if s.title == "异常未了结")
        self.assertEqual(section.total, 1)
        self.assertEqual(section.items[0].order_no, order.order_no)


# --------------------------------------------------------------------------- #
class TestScopeIsolation(BriefingTestCase):
    def test_sales_does_not_see_other_sales_overdue(self):
        other = self.make_order(self.sales_b, status=Status.PURCHASING, days_ago=10, desc="乙的卡单")
        briefing = self.build_for(self.sales_a)

        self.assertNotIn("乙的卡单", briefing.text)
        self.assertNotIn(other.order_no, briefing.text)
        # 连"卡住了"这句话都不该出现
        self.assertIn("没有卡单", briefing.headline)

    def test_admin_sees_everyones(self):
        self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=10, desc="甲的卡单")
        self.make_order(self.sales_b, status=Status.PURCHASING, days_ago=9, desc="乙的卡单")
        briefing = self.build_for(self.admin_id)
        self.assertIn("2 单卡住", briefing.headline)
        self.assertIn("甲的卡单", briefing.text)
        self.assertIn("乙的卡单", briefing.text)

    def test_sales_scope_only_counts_own(self):
        self.make_order(self.sales_a, status=Status.WAITING_SETTLEMENT)
        self.make_order(self.sales_b, status=Status.WAITING_SETTLEMENT)
        section = next(s for s in self.build_for(self.sales_a).sections if s.title == "待你结算")
        self.assertEqual(section.total, 1)


# --------------------------------------------------------------------------- #
class TestTextRendering(BriefingTestCase):
    def test_text_is_paste_ready(self):
        self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=10)
        text = self.build_for(self.admin_id).text
        self.assertIn("【日代 ERP · 日报】", text)
        self.assertIn("范围：全公司", text)
        self.assertIn("■ 卡住了（1 单）", text)
        self.assertIn("20261010-001", text)
        self.assertIn("等：", text)
        self.assertNotIn("None", text)

    def test_spells_out_empty_sections(self):
        self.assertIn("· 无", self.build_for(self.admin_id).text)


# --------------------------------------------------------------------------- #
class TestBriefingPage(BriefingTestCase):
    def test_requires_login(self):
        response = self.client.get("/briefing", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_renders_headline_and_copy_button(self):
        self.make_order(self.sales_a, status=Status.PURCHASING, days_ago=10)
        self.login("admin")
        page = self.client.get("/briefing")
        self.assertEqual(page.status_code, 200)
        self.assertIn("复制日报", page.text)
        self.assertIn("1 单卡住", page.text)
        self.assertIn("各状态堆积", page.text)

    def test_page_does_not_leak_other_scope(self):
        secret = self.make_order(self.sales_b, status=Status.PURCHASING, days_ago=10, desc="乙的绝密单")
        self.login("sales_a")
        page = self.client.get("/briefing")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("乙的绝密单", page.text)
        self.assertNotIn(secret.order_no, page.text)

    def test_nav_has_briefing_link(self):
        self.login("admin")
        self.assertIn('href="/briefing"', self.client.get("/").text)


if __name__ == "__main__":
    unittest.main()
