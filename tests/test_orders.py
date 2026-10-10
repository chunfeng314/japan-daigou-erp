"""订单模块测试：订单号、建单、可见性、编辑、截图上传。

⚠️ 本模块**显式改写 config.DB_PATH** 再 reset 引擎，不依赖环境变量 ——
配置在导入时就被读走了，只靠 env 会被同进程里先导入的测试模块"抢跑"。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-orders-test-"))
_ORIGINAL_DB_PATH = config.DB_PATH
_ORIGINAL_UPLOAD_DIR = config.UPLOAD_DIR
config.DB_PATH = _TMP_DIR / "orders.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"

reset_state()
init_db()

from app.main import app  # noqa: E402
from app.models import Order, OrderImage, ProductHistory, StatusLog, User  # noqa: E402
from app.order_service import create_order, generate_order_no, recent_products  # noqa: E402
from app.permissions import can_edit_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role, Status  # noqa: E402
from tests.support import wipe_all  # noqa: E402

PASSWORD = "pw123456"

#: 1x1 的合法 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF/AIl8AAAAAElFTkSuQmCC"
)


def _create_user(username: str, role: str, display_name: str, is_active: int = 1) -> int:
    session = get_sessionmaker()()
    try:
        user = User(
            username=username,
            display_name=display_name,
            role=role,
            password_hash=hash_password(PASSWORD),
            is_active=is_active,
        )
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


def _session():
    return get_sessionmaker()()


class OrderTestCase(unittest.TestCase):
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
        self.wh_id = _create_user("wh", Role.WAREHOUSE.value, "上海仓")
        self.client.cookies.clear()

    # ---------- 工具 ----------
    def login(self, username: str):
        response = self.client.post(
            "/login",
            data={"username": username, "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        return response

    def make_order(self, salesperson_id: int, **kwargs) -> Order:
        session = _session()
        try:
            owner = session.get(User, salesperson_id)
            order = create_order(
                session,
                salesperson=owner,
                order_date=kwargs.pop("order_date", date(2026, 10, 10)),
                product_desc=kwargs.pop("product_desc", "限定 香水 100ml"),
                quantity=kwargs.pop("quantity", 1),
                sale_amount=kwargs.pop("sale_amount", 4200.0),
                **kwargs,
            )
            return order
        finally:
            session.close()

    def post_new_order(self, **overrides):
        payload = {
            "platform": "Mercari",
            "shop": "本店",
            "order_date": "2026-10-10",
            "order_time": "21:30",
            "product_desc": "限定 香水 100ml",
            "quantity": "2",
            "sale_amount": "8400",
            "sale_currency": "JPY",
            "image_keys": "[]",
            "image_names": "[]",
        }
        payload.update(overrides)
        return self.client.post("/orders/new", data=payload, follow_redirects=False)

    def assert_listed(self, html: str, order: Order):
        """断言订单出现在列表里 —— 认详情页链接，比认文本身份更稳。"""
        self.assertIn(f'href="/orders/{order.id}"', html, f"{order.order_no} 应出现在列表里")

    def assert_not_listed(self, html: str, order: Order):
        self.assertNotIn(f'href="/orders/{order.id}"', html, f"{order.order_no} 不该出现在列表里")


# --------------------------------------------------------------------------- #
class TestOrderNumber(OrderTestCase):
    def test_first_number_of_day(self):
        session = _session()
        try:
            self.assertEqual(generate_order_no(session, date(2026, 10, 10)), "20261010-001")
        finally:
            session.close()

    def test_increments_within_day(self):
        self.make_order(self.sales_a)
        session = _session()
        try:
            self.assertEqual(generate_order_no(session, date(2026, 10, 10)), "20261010-002")
        finally:
            session.close()

    def test_resets_next_day(self):
        self.make_order(self.sales_a, order_date=date(2026, 10, 10))
        session = _session()
        try:
            self.assertEqual(generate_order_no(session, date(2026, 10, 11)), "20261011-001")
        finally:
            session.close()

    def test_unique_constraint_holds(self):
        first = self.make_order(self.sales_a)
        second = self.make_order(self.sales_a)
        third = self.make_order(self.sales_b)
        self.assertEqual(len({first.order_no, second.order_no, third.order_no}), 3)


# --------------------------------------------------------------------------- #
class TestCreateOrderService(OrderTestCase):
    def test_writes_log_history_and_images(self):
        order = self.make_order(
            self.sales_a,
            image_keys=["2026/10/aaa.png", "2026/10/bbb.png"],
            image_names=["a.png", "b.png"],
        )
        session = _session()
        try:
            self.assertEqual(order.status, Status.PENDING_PURCHASE.value)
            logs = session.scalars(select(StatusLog).where(StatusLog.order_id == order.id)).all()
            self.assertEqual(len(logs), 1)
            self.assertIsNone(logs[0].from_status)
            self.assertEqual(logs[0].to_status, Status.PENDING_PURCHASE.value)

            images = session.scalars(
                select(OrderImage).where(OrderImage.order_id == order.id)
            ).all()
            self.assertEqual([i.cos_key for i in images], ["2026/10/aaa.png", "2026/10/bbb.png"])
            self.assertEqual(images[0].filename, "a.png")

            history = session.scalars(select(ProductHistory)).all()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].use_count, 1)
        finally:
            session.close()

    def test_history_accumulates_and_dedupes(self):
        self.make_order(self.sales_a, product_desc="同款 手办")
        self.make_order(self.sales_a, product_desc="同款 手办")
        session = _session()
        try:
            rows = recent_products(session, self.sales_a)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].use_count, 2)
        finally:
            session.close()

    def test_history_is_per_user(self):
        self.make_order(self.sales_a, product_desc="甲的商品")
        self.make_order(self.sales_b, product_desc="乙的商品")
        session = _session()
        try:
            self.assertEqual([r.product_desc for r in recent_products(session, self.sales_a)], ["甲的商品"])
            self.assertEqual([r.product_desc for r in recent_products(session, self.sales_b)], ["乙的商品"])
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestCreateViaHttp(OrderTestCase):
    def test_sales_can_create(self):
        self.login("sales_a")
        response = self.post_new_order()
        self.assertEqual(response.status_code, 303)
        self.assertIn("/orders/", response.headers["location"])
        self.assertIn("msg=", response.headers["location"])

        session = _session()
        try:
            order = session.scalars(select(Order)).one()
            self.assertEqual(order.order_no, "20261010-001")
            self.assertEqual(order.quantity, 2)
            self.assertEqual(order.sale_amount, 8400.0)
            self.assertEqual(order.platform, "Mercari")
            self.assertEqual(order.salesperson_id, self.sales_a)
        finally:
            session.close()

    def test_validation_rejects_bad_input(self):
        self.login("sales_a")
        response = self.post_new_order(product_desc="  ", quantity="0", sale_amount="abc")
        self.assertEqual(response.status_code, 400)
        self.assertIn("商品需求/名称不能为空", response.text)
        self.assertIn("数量必须大于 0", response.text)
        self.assertIn("销售金额必须是数字", response.text)

    def test_purchaser_cannot_open_form(self):
        self.login("buyer")
        self.assertEqual(self.client.get("/orders/new", follow_redirects=False).status_code, 403)

    def test_warehouse_cannot_open_form(self):
        self.login("wh")
        self.assertEqual(self.client.get("/orders/new", follow_redirects=False).status_code, 403)

    def test_anonymous_is_redirected(self):
        response = self.client.get("/orders/new", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))


# --------------------------------------------------------------------------- #
class TestVisibility(OrderTestCase):
    def test_sales_sees_own_only(self):
        mine = self.make_order(self.sales_a, product_desc="甲的单")
        other = self.make_order(self.sales_b, product_desc="乙的单")

        self.login("sales_a")
        page = self.client.get("/orders")
        self.assertEqual(page.status_code, 200)
        self.assert_listed(page.text, mine)
        self.assertIn("甲的单", page.text)
        self.assert_not_listed(page.text, other)
        self.assertNotIn("乙的单", page.text)

    def test_sales_cannot_open_other_detail(self):
        other = self.make_order(self.sales_b)
        self.login("sales_a")
        self.assertEqual(self.client.get(f"/orders/{other.id}").status_code, 403)

    def test_sales_can_open_own_detail(self):
        mine = self.make_order(self.sales_a)
        self.login("sales_a")
        page = self.client.get(f"/orders/{mine.id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(mine.order_no, page.text)

    def test_purchaser_sees_pending_orders(self):
        order = self.make_order(self.sales_a)
        self.login("buyer")
        self.assert_listed(self.client.get("/orders").text, order)

    def test_purchaser_stops_seeing_after_status_moves(self):
        order = self.make_order(self.sales_a)
        session = _session()
        try:
            row = session.get(Order, order.id)
            row.status = Status.PURCHASING.value
            session.commit()
        finally:
            session.close()

        self.login("buyer")
        self.assert_not_listed(self.client.get("/orders").text, order)

    def test_warehouse_default_hides_pending_but_search_finds_it(self):
        order = self.make_order(self.sales_a)
        self.login("wh")

        self.assert_not_listed(self.client.get("/orders").text, order)
        self.assert_listed(self.client.get(f"/orders?q={order.order_no}").text, order)

    def test_warehouse_sees_receiving_queue(self):
        order = self.make_order(self.sales_a)
        session = _session()
        try:
            row = session.get(Order, order.id)
            row.status = Status.WAITING_SH_RECEIPT.value
            session.commit()
        finally:
            session.close()

        self.login("wh")
        self.assert_listed(self.client.get("/orders").text, order)

    def test_admin_sees_everything(self):
        a = self.make_order(self.sales_a)
        b = self.make_order(self.sales_b)
        self.login("admin")
        page = self.client.get("/orders")
        self.assert_listed(page.text, a)
        self.assert_listed(page.text, b)


# --------------------------------------------------------------------------- #
class TestEdit(OrderTestCase):
    def test_owner_can_edit(self):
        order = self.make_order(self.sales_a, product_desc="原标题")
        self.login("sales_a")
        response = self.client.post(
            f"/orders/{order.id}/edit",
            data={
                "platform": "Mercari",
                "shop": "本店",
                "order_date": order.order_date.isoformat(),
                "order_time": "",
                "product_desc": "改后标题",
                "quantity": "3",
                "sale_amount": "9999",
                "sale_currency": "JPY",
                "image_keys": "[]",
                "image_names": "[]",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        session = _session()
        try:
            row = session.get(Order, order.id)
            self.assertEqual(row.product_desc, "改后标题")
            self.assertEqual(row.quantity, 3)
            self.assertEqual(row.sale_amount, 9999.0)
            note = session.scalars(
                select(StatusLog).where(StatusLog.order_id == order.id).order_by(StatusLog.id)
            ).all()[-1]
            self.assertIn("修改订单信息", note.note)
            self.assertIn("商品需求", note.note)
        finally:
            session.close()

    def test_other_sales_cannot_edit(self):
        order = self.make_order(self.sales_a)
        self.login("sales_b")
        self.assertEqual(self.client.get(f"/orders/{order.id}/edit").status_code, 403)

    def test_cannot_edit_completed(self):
        order = self.make_order(self.sales_a)
        session = _session()
        try:
            row = session.get(Order, order.id)
            row.status = Status.COMPLETED.value
            session.commit()
            self.assertFalse(can_edit_order(row.salesperson, row))
        finally:
            session.close()

        self.login("sales_a")
        self.assertEqual(self.client.get(f"/orders/{order.id}/edit").status_code, 403)

    def test_edit_form_is_prefilled(self):
        order = self.make_order(self.sales_a, product_desc="预填检查")
        self.login("sales_a")
        page = self.client.get(f"/orders/{order.id}/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn("预填检查", page.text)


# --------------------------------------------------------------------------- #
class TestUploads(OrderTestCase):
    def test_upload_requires_login(self):
        response = self.client.post(
            "/api/uploads",
            files={"files": ("a.png", PNG_BYTES, "image/png")},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_upload_returns_key_and_owner_can_read(self):
        self.login("sales_a")
        response = self.client.post(
            "/api/uploads",
            files={"files": ("a.png", PNG_BYTES, "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["mode"], "local")
        self.assertEqual(len(body["files"]), 1)
        key = body["files"][0]["key"]
        self.assertTrue(key.endswith(".png"))

        order = self.make_order(self.sales_a, image_keys=[key], image_names=["a.png"])
        read = self.client.get(f"/files/{key}")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.content, PNG_BYTES)

        # 别的销售看不到这单的截图
        self.client.cookies.clear()
        self.login("sales_b")
        self.assertEqual(self.client.get(f"/files/{key}").status_code, 403)

        session = _session()
        try:
            self.assertEqual(
                session.scalars(select(OrderImage).where(OrderImage.order_id == order.id)).one().cos_key,
                key,
            )
        finally:
            session.close()

    def test_upload_rejects_wrong_extension(self):
        self.login("sales_a")
        response = self.client.post(
            "/api/uploads",
            files={"files": ("evil.txt", b"hello", "text/plain")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持的文件类型", response.json()["detail"])

    def test_upload_rejects_oversize(self):
        original = config.MAX_UPLOAD_BYTES
        config.MAX_UPLOAD_BYTES = 10
        try:
            self.login("sales_a")
            response = self.client.post(
                "/api/uploads",
                files={"files": ("big.png", PNG_BYTES * 10, "image/png")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("上限", response.json()["detail"])
        finally:
            config.MAX_UPLOAD_BYTES = original

    def test_sign_endpoint_returns_local_mode(self):
        self.login("sales_a")
        response = self.client.post(
            "/api/uploads/sign", json={"filename": "a.png", "content_type": "image/png"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "local")
        self.assertIsNone(body["url"])
        self.assertTrue(body["key"].endswith(".png"))

    def test_file_path_traversal_blocked(self):
        self.login("sales_a")
        response = self.client.get("/files/../../etc/passwd")
        self.assertIn(response.status_code, (400, 403, 404))


# --------------------------------------------------------------------------- #
class TestProductHistoryApi(OrderTestCase):
    def test_returns_recent_for_current_user(self):
        self.make_order(self.sales_a, product_desc="限定 香水")
        self.login("sales_a")
        response = self.client.get("/api/product-history")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([i["desc"] for i in items], ["限定 香水"])
        self.assertEqual(items[0]["use_count"], 1)

    def test_keyword_filters(self):
        self.make_order(self.sales_a, product_desc="限定 香水")
        self.make_order(self.sales_a, product_desc="普通 手办")
        self.login("sales_a")

        filtered = self.client.get("/api/product-history?q=手办").json()["items"]
        self.assertEqual([i["desc"] for i in filtered], ["普通 手办"])


if __name__ == "__main__":
    unittest.main()
