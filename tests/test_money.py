"""金额表达与汇总测试。

守住两条底线（开发文档 §2.4）：
1. **绝不跨币种相加**
2. **不算利润**（页面与代码里都不该出现"利润/汇率"）
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-money-test-"))
config.DB_PATH = _TMP_DIR / "money.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"

reset_state()
init_db()

from app.models import Purchase, Settlement, User  # noqa: E402
from app.money import (  # noqa: E402
    collect,
    currency_label,
    format_money,
)
from app.order_service import create_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role  # noqa: E402
from tests.support import wipe_all  # noqa: E402

PASSWORD = "pw123456"


# --------------------------------------------------------------------------- #
class TestFormatMoney(unittest.TestCase):
    def test_jpy_has_no_decimals(self):
        self.assertEqual(format_money(8400, "JPY"), "¥8,400 日元")
        self.assertEqual(format_money(8400.4, "JPY"), "¥8,400 日元")

    def test_cny_has_two_decimals_and_thousands(self):
        self.assertEqual(format_money(128.5, "CNY"), "￥128.50 元")
        self.assertEqual(format_money(12345.678, "CNY"), "￥12,345.68 元")

    def test_none_is_dash(self):
        self.assertEqual(format_money(None, "CNY"), "—")

    def test_unknown_currency_falls_back_to_code(self):
        self.assertEqual(format_money(100, "USD"), "100.00 USD")

    def test_no_currency(self):
        self.assertEqual(format_money(100, None), "100.00")

    def test_currency_label_is_full_name(self):
        """分组标题要用全名 ——「支出（元）」看不懂，「支出（人民币）」才行。"""
        self.assertEqual(currency_label("CNY"), "人民币")
        self.assertEqual(currency_label("JPY"), "日元")


# --------------------------------------------------------------------------- #
class TestCollect(unittest.TestCase):
    def setUp(self) -> None:
        wipe_all()
        session = get_sessionmaker()()
        try:
            self.sales = User(
                username="s", display_name="销售", role=Role.SALES.value,
                password_hash=hash_password(PASSWORD), is_active=1,
            )
            session.add(self.sales)
            session.commit()
            session.refresh(self.sales)
            self.order = create_order(
                session, salesperson=self.sales, order_date=date(2026, 10, 10),
                product_desc="商品", quantity=1, sale_amount=8400.0,
            )
            session.commit()
            session.refresh(self.order)
        finally:
            session.close()

    def _add_purchase(self, price=128.0, fee=120.0, other=8.0, currency="CNY"):
        session = get_sessionmaker()()
        try:
            session.add(Purchase(
                order_id=self.order.id, purchase_price=price, purchase_currency=currency,
                source_platform="淘宝", carrier="中通",
                domestic_shipping_fee=fee, other_fee=other,
            ))
            session.commit()
        finally:
            session.close()

    def _add_settlement(self, final=3453.0, fee=323.0, jp_ship=123.0,
                        currency="JPY", refund=0.0, deduction=0.0):
        session = get_sessionmaker()()
        try:
            session.add(Settlement(
                order_id=self.order.id, settle_date=date(2026, 10, 20),
                final_amount=final, currency=currency,
                platform_fee=fee, japan_shipping=jp_ship, refund=refund,
                other_deduction=deduction,
            ))
            session.commit()
        finally:
            session.close()

    def test_totals_are_split_by_currency(self):
        """★ 核心底线：人民币和日元**绝不相加**。"""
        self._add_purchase()
        self._add_settlement()
        session = get_sessionmaker()()
        try:
            money = collect(session, self.order)
        finally:
            session.close()

        totals = dict(money.totals_by_currency)
        self.assertEqual(totals["CNY"], 256.0)     # 128 + 120 + 8
        self.assertEqual(totals["JPY"], 446.0)     # 323 + 123
        self.assertEqual(len(totals), 2, "不该出现跨币种合并")

    def test_zero_amounts_are_dropped(self):
        self._add_purchase(fee=0.0, other=0.0)
        session = get_sessionmaker()()
        try:
            money = collect(session, self.order)
        finally:
            session.close()
        labels = [item.label for item in money.expenses]
        self.assertIn("采购成本", labels)
        self.assertNotIn("国内运费", labels)
        self.assertNotIn("其他费用", labels)

    def test_no_purchase_yet(self):
        session = get_sessionmaker()()
        try:
            money = collect(session, self.order)
        finally:
            session.close()
        self.assertFalse(money.has_any)
        self.assertEqual(money.totals_by_currency, ())
        self.assertEqual(money.sale.amount, 8400.0)

    def test_settle_uses_its_own_currency(self):
        """结算侧的所有费用都按**结算币种**归集，不会另起一堆。

        注意：日本境内运费也跟着结算币种走 —— 现实中结算币种本来就是日元，
        所以这个"跟随"是对的；只有把结算币种误填成人民币时才会看到它并进人民币小计。
        """
        self._add_purchase()                                   # CNY 128 + 120 + 8 = 256
        self._add_settlement(currency="CNY", fee=50.0, jp_ship=123.0)
        session = get_sessionmaker()()
        try:
            money = collect(session, self.order)
        finally:
            session.close()
        # 256 + 50（手续费）+ 123（日本境内运费）= 429，且只有一个币种分组
        totals = dict(money.totals_by_currency)
        self.assertEqual(totals, {"CNY": 429.0})

    def test_expenses_carry_source_notes(self):
        self._add_purchase()
        session = get_sessionmaker()()
        try:
            money = collect(session, self.order)
        finally:
            session.close()
        notes = {item.label: item.note for item in money.expenses}
        self.assertEqual(notes["采购成本"], "淘宝")
        self.assertEqual(notes["国内运费"], "中通")


# --------------------------------------------------------------------------- #
class TestMoneyPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(__import__("app.main", fromlist=["app"]).app)

    def setUp(self) -> None:
        wipe_all()
        session = get_sessionmaker()()
        try:
            admin = User(username="admin", display_name="管理员", role=Role.ADMIN.value,
                         password_hash=hash_password(PASSWORD), is_active=1)
            sales = User(username="s", display_name="销售", role=Role.SALES.value,
                         password_hash=hash_password(PASSWORD), is_active=1)
            session.add_all([admin, sales])
            session.commit()
            session.refresh(admin)
            session.refresh(sales)
            self.admin_name = admin.username
            order = create_order(
                session, salesperson=sales, order_date=date(2026, 10, 10),
                product_desc="商品", quantity=1, sale_amount=8400.0,
            )
            session.add(Purchase(order_id=order.id, purchase_price=128.0,
                                 purchase_currency="CNY", source_platform="淘宝",
                                 carrier="中通", domestic_shipping_fee=120.0, other_fee=8.0))
            session.commit()
            self.order_id = order.id
        finally:
            session.close()
        self.client.cookies.clear()
        response = self.client.post(
            "/login",
            data={"username": self.admin_name, "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_detail_page_has_money_card(self):
        page = self.client.get(f"/orders/{self.order_id}").text
        self.assertIn("这单的钱", page)
        self.assertIn("￥128.00 元", page)
        self.assertIn("￥120.00 元", page)
        self.assertIn("支出（人民币）", page)
        self.assertIn("￥256.00 元", page)      # 小计
        self.assertIn("¥8,400 日元", page)      # 售价

    def test_stage_blocks_now_show_currency(self):
        """原来「国内运费 120.00」是裸数字，看不出是什么钱。"""
        page = self.client.get(f"/orders/{self.order_id}").text
        self.assertIn("￥120.00 元", page)
        self.assertNotIn("国内运费 120.00", page)

    def test_page_never_shows_profit_or_fx(self):
        """铁律：不算利润、不换汇 —— 页面上不该出现这些承诺。"""
        page = self.client.get(f"/orders/{self.order_id}").text
        self.assertNotIn("利润", page.replace("也不计算利润", ""))
        self.assertNotIn("汇率", page)

    def test_empty_money_card_explains_itself(self):
        session = get_sessionmaker()()
        try:
            sales = session.get(User, 2) or session.scalars(
                __import__("sqlalchemy").select(User).where(User.role == Role.SALES.value)
            ).first()
            order = create_order(
                session, salesperson=sales, order_date=date(2026, 10, 10),
                product_desc="还没采购", quantity=1, sale_amount=1000.0,
            )
            session.commit()
            fresh_id = order.id
        finally:
            session.close()
        page = self.client.get(f"/orders/{fresh_id}").text
        self.assertIn("还没登记任何成本", page)


if __name__ == "__main__":
    unittest.main()
