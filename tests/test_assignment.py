"""采购分配测试。

规则（`orders.assigned_purchaser_id`）：
- **未指定** = 先接先得，所有采购都能在待采购阶段看到
- **指定了某人** = 只有他在待采购阶段能看到（别人不白点一场）
- 一旦有人点了「开始采购」，就以 `purchases.purchaser_id` 为准，
  指派的人如果没经手，之后也看不到了（避免"我是负责人但看不见"的困惑）
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-assign-test-"))
config.DB_PATH = _TMP_DIR / "assign.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"

reset_state()
init_db()

from app.models import Order, Purchase, User  # noqa: E402
from app.order_service import create_order  # noqa: E402
from app.permissions import can_purchaser_take  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role, Status  # noqa: E402
from app.transition_service import advance_order  # noqa: E402
from tests.support import wipe_all  # noqa: E402

PASSWORD = "pw123456"


def _user(username: str, role: str, display: str) -> User:
    session = get_sessionmaker()()
    try:
        user = User(
            username=username,
            display_name=display,
            role=role,
            password_hash=hash_password(PASSWORD),
            is_active=1,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user
    finally:
        session.close()


def _session():
    return get_sessionmaker()()


class AssignmentTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(__import__("app.main", fromlist=["app"]).app)

    def setUp(self) -> None:
        wipe_all()
        self.admin = _user("admin", Role.ADMIN.value, "管理员")
        self.sales = _user("sales", Role.SALES.value, "销售甲")
        self.p1 = _user("p1", Role.PURCHASER.value, "采购一")
        self.p2 = _user("p2", Role.PURCHASER.value, "采购二")
        self.wh = _user("wh", Role.WAREHOUSE.value, "仓管")
        self.client.cookies.clear()

    def make_order(self, assigned: User | None = None, desc: str = "限量手办") -> Order:
        session = _session()
        try:
            return create_order(
                session,
                salesperson=session.get(User, self.sales.id),
                order_date=date(2026, 10, 10),
                product_desc=desc,
                quantity=1,
                sale_amount=100.0,
                assigned_purchaser=assigned,
            )
        finally:
            session.close()

    def _as_user(self, user: User) -> User:
        """把 setUp 里保存的 User 换成当前 session 里的（测试里最常见的踩坑）。"""
        session = _session()
        try:
            return session.get(User, user.id)
        finally:
            session.close()

    def login(self, username: str, password: str = PASSWORD) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": password, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)


# --------------------------------------------------------------------------- #
class TestVisibility(AssignmentTestCase):
    def test_unassigned_is_visible_to_all_purchasers(self):
        order = self.make_order(assigned=None)
        for buyer in (self.p1, self.p2):
            self.login(buyer.username)
            self.assertIn(order.order_no, self.client.get("/orders").text)

    def test_assigned_order_only_visible_to_that_purchaser(self):
        order = self.make_order(assigned=self._as_user(self.p1))

        self.login("p1")
        self.assertIn(order.order_no, self.client.get("/orders").text)
        self.assertEqual(self.client.get(f"/orders/{order.id}").status_code, 200)

        self.login("p2")
        self.assertNotIn(order.order_no, self.client.get("/orders").text)
        # 详情也进不去 —— 别让人靠直链试探
        self.assertEqual(self.client.get(f"/orders/{order.id}").status_code, 403)

    def _as_user(self, user: User) -> User:
        session = _session()
        try:
            return session.get(User, user.id)
        finally:
            session.close()

    def test_admin_and_sales_still_see_everything(self):
        order = self.make_order(assigned=self._as_user(self.p1))
        self.login("admin")
        self.assertIn(order.order_no, self.client.get("/orders").text)
        self.login("sales")
        self.assertIn(order.order_no, self.client.get("/orders").text)

    def test_after_purchase_starts_visibility_follows_handler(self):
        """一旦有人开始采购，指派但没经手的人就不该再看到（避免"我是负责人却看不见"）。"""
        order = self.make_order(assigned=self._as_user(self.p1))
        session = _session()
        try:
            target = session.get(Order, order.id)
            result = advance_order(
                session, order=target, to_status=Status.PURCHASING, operator=session.get(User, self.p2.id)
            )
            self.assertTrue(result.ok, result.message)
            session.commit()
        finally:
            session.close()

        self.login("p2")
        self.assertIn(order.order_no, self.client.get("/orders").text)
        self.login("p1")   # 指派人但没经手
        self.assertNotIn(order.order_no, self.client.get("/orders").text)

    def test_can_purchaser_take_helper(self):
        session = _session()
        try:
            unassigned = session.get(Order, self.make_order().id)
            mine = session.get(Order, self.make_order(assigned=self._as_user(self.p1)).id)
            self.assertTrue(can_purchaser_take(unassigned, session.get(User, self.p2.id)))
            self.assertFalse(can_purchaser_take(mine, session.get(User, self.p2.id)))
            self.assertTrue(can_purchaser_take(mine, session.get(User, self.p1.id)))
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestCreateForm(AssignmentTestCase):
    def _create(self, **extra) -> str:
        payload = {
            "order_date": "2026-10-10",
            "product_desc": "限量手办",
            "quantity": "1",
            "sale_amount": "4200",
            "sale_currency": "JPY",
            "image_keys": "[]",
            "image_names": "[]",
        }
        payload.update(extra)
        return payload

    def test_form_offers_purchasers(self):
        self.login("sales")
        page = self.client.get("/orders/new").text
        self.assertIn('name="assigned_purchaser_id"', page)
        self.assertIn("采购一", page)
        self.assertIn("不指定", page)

    def test_creating_with_assignment(self):
        self.login("sales")
        response = self.client.post(
            "/orders/new", data=self._create(assigned_purchaser_id=str(self.p1.id)),
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        session = _session()
        try:
            order = session.get(Order, int(response.headers["location"].split("/orders/")[1].split("?")[0]))
            self.assertEqual(order.assigned_purchaser_id, self.p1.id)
        finally:
            session.close()

    def test_creating_without_assignment(self):
        self.login("sales")
        response = self.client.post(
            "/orders/new", data=self._create(), follow_redirects=False
        )
        session = _session()
        try:
            order = session.get(Order, int(response.headers["location"].split("/orders/")[1].split("?")[0]))
            self.assertIsNone(order.assigned_purchaser_id)
        finally:
            session.close()

    def test_cannot_assign_to_non_purchaser(self):
        """把单指派给销售/仓管会让它从所有采购视野里消失 —— 必须拒绝。"""
        for target in (self.sales, self.wh, self.admin):
            self.login("sales")
            self.client.post(
                "/orders/new",
                data=self._create(assigned_purchaser_id=str(target.id)),
                follow_redirects=False,
            )
        session = _session()
        try:
            orders = session.scalars(select(Order)).all()
            self.assertTrue(orders)
            for order in orders:
                self.assertIsNone(
                    order.assigned_purchaser_id, "不该把单指派给非采购角色"
                )
        finally:
            session.close()

    def test_cannot_assign_to_deactivated_purchaser(self):
        session = _session()
        try:
            row = session.get(User, self.p2.id)
            row.is_active = 0
            session.commit()
        finally:
            session.close()

        self.login("sales")
        response = self.client.post(
            "/orders/new", data=self._create(assigned_purchaser_id=str(self.p2.id)),
            follow_redirects=False,
        )
        session = _session()
        try:
            order = session.get(Order, int(response.headers["location"].split("/orders/")[1].split("?")[0]))
            self.assertIsNone(order.assigned_purchaser_id)
        finally:
            session.close()

    def test_garbage_value_is_ignored(self):
        self.login("sales")
        response = self.client.post(
            "/orders/new", data=self._create(assigned_purchaser_id="../../etc/passwd"),
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)


# --------------------------------------------------------------------------- #
class TestDisplay(AssignmentTestCase):
    def test_list_shows_purchaser_column(self):
        order = self.make_order(assigned=self._as_user(self.p1))
        self.login("admin")
        page = self.client.get("/orders").text
        self.assertIn("采购负责人" if "采购负责人" in page else "采购", page)
        self.assertIn("采购一", page)
        self.assertNotIn(">采购二<", page)

    def test_detail_shows_owner(self):
        order = self.make_order(assigned=self._as_user(self.p1))
        self.login("sales")
        page = self.client.get(f"/orders/{order.id}").text
        self.assertIn("采购负责人", page)
        self.assertIn("采购一", page)

    def test_detail_shows_unassigned_hint(self):
        order = self.make_order(assigned=None)
        self.login("sales")
        page = self.client.get(f"/orders/{order.id}").text
        self.assertIn("未分配", page)

    def test_csv_has_purchaser_column(self):
        self.make_order(assigned=self._as_user(self.p1))
        self.login("admin")
        text = self.client.get("/orders/export.csv").content.decode("utf-8").lstrip("﻿")
        header = text.splitlines()[0]
        self.assertIn("采购负责人", header)
        self.assertIn("采购一", text)


# --------------------------------------------------------------------------- #
class TestAssignmentChange(AssignmentTestCase):
    def test_can_reassign_before_purchase_starts(self):
        order = self.make_order(assigned=self._as_user(self.p1))
        self.login("sales")
        response = self.client.post(
            f"/orders/{order.id}/edit",
            data={
                "order_date": "2026-10-10", "product_desc": "限量手办", "quantity": "1",
                "sale_amount": "4200", "sale_currency": "JPY",
                "assigned_purchaser_id": str(self.p2.id),
                "image_keys": "[]", "image_names": "[]",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        session = _session()
        try:
            self.assertEqual(session.get(Order, order.id).assigned_purchaser_id, self.p2.id)
        finally:
            session.close()

    def test_cannot_reassign_after_purchase_started(self):
        """订单已经有人在跟了，悄悄换人会让"谁负责"说不清。"""
        order = self.make_order(assigned=self._as_user(self.p1))
        session = _session()
        try:
            advance_order(
                session, order=session.get(Order, order.id),
                to_status=Status.PURCHASING, operator=session.get(User, self.p1.id),
            )
            session.commit()
        finally:
            session.close()

        self.login("sales")
        self.client.post(
            f"/orders/{order.id}/edit",
            data={
                "order_date": "2026-10-10", "product_desc": "限量手办", "quantity": "1",
                "sale_amount": "4200", "sale_currency": "JPY",
                "assigned_purchaser_id": str(self.p2.id),
                "image_keys": "[]", "image_names": "[]",
            },
            follow_redirects=False,
        )
        session = _session()
        try:
            self.assertEqual(
                session.get(Order, order.id).assigned_purchaser_id,
                self.p1.id,
                "开始采购后不该再能改指派",
            )
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestMigration(unittest.TestCase):
    def test_additive_migration_adds_column_to_existing_table(self):
        """SQLite 的 create_all 不会给已有表加列 —— 老库必须靠迁移补上。"""
        import tempfile as _tempfile

        workdir = Path(_tempfile.mkdtemp(prefix="erp-migrate-"))
        db_path = workdir / "old.sqlite3"
        # 造一个"老库"：有 orders 表，但没有 assigned_purchaser_id
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, order_no TEXT, status TEXT)"
            )
            connection.execute("INSERT INTO orders (order_no, status) VALUES ('20261010-001', '待采购')")
            connection.commit()
        finally:
            connection.close()

        from app.db import get_engine

        original = config.DB_PATH
        config.DB_PATH = db_path
        reset_state()
        try:
            init_db()   # 内部会 create_all + apply_migrations

            connection = sqlite3.connect(db_path)
            try:
                cols = [row[1] for row in connection.execute("PRAGMA table_info(orders)")]
                rows = connection.execute("SELECT order_no, status FROM orders").fetchall()
            finally:
                connection.close()
            self.assertIn("assigned_purchaser_id", cols)
            # 老数据必须完好
            self.assertEqual(rows, [("20261010-001", "待采购")])
        finally:
            config.DB_PATH = original
            reset_state()
            init_db()

    def test_migration_is_idempotent(self):
        """重复跑 init_db 不能报错、也不能重复加列。"""
        init_db()
        init_db()
        connection = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            cols = [row[1] for row in connection.execute("PRAGMA table_info(orders)")]
        finally:
            connection.close()
        self.assertEqual(cols.count("assigned_purchaser_id"), 1)


if __name__ == "__main__":
    unittest.main()
