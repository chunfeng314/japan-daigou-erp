"""截图字段提取测试。

这里测的不是"认得准不准"（那是实弹测试的事），而是**模型输出不可信时会不会出错**：
坏 JSON、围栏、多余字段、类型不对、日期格式错 —— 一律退化成"没识别到"，
**绝不能让错值流进表单**。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-extract-test-"))
config.DB_PATH = _TMP_DIR / "extract.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"
# 清掉模型凭据：单元测试一律不联网（实弹测试在 test_llm_live.py）
config.LLM_BASE_URL = ""
config.LLM_API_KEY = ""
config.LLM_MODEL = ""

reset_state()
init_db()

from app.agent.extract import (  # noqa: E402
    ALL_FIELDS,
    extract_order_fields,
    image_content,
    parse_extraction,
)
from app.agent.llm import LLMReply, ScriptedLLM  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role  # noqa: E402
from tests.support import wipe_all  # noqa: E402

PASSWORD = "pw123456"
GOOD = (
    '{"platform": "Mercari", "shop": "本店", "order_date": "2026-10-10", "order_time": "21:30", '
    '"product_desc": "限定 香水 100ml", "quantity": 2, "sale_amount": 8400, "sale_currency": "JPY"}'
)


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


# --------------------------------------------------------------------------- #
class TestParsingHappyPath(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            parse_extraction(GOOD),
            {
                "platform": "Mercari",
                "shop": "本店",
                "order_date": "2026-10-10",
                "order_time": "21:30",
                "product_desc": "限定 香水 100ml",
                "quantity": 2,
                "sale_amount": 8400.0,
                "sale_currency": "JPY",
            },
        )

    def test_markdown_fence_is_stripped(self):
        self.assertEqual(parse_extraction(f"```json\n{GOOD}\n```")["platform"], "Mercari")

    def test_fence_without_language(self):
        self.assertEqual(parse_extraction(f"```\n{GOOD}\n```")["shop"], "本店")

    def test_prose_around_json(self):
        text = f"我看看这张图。\n{GOOD}\n以上就是识别结果。"
        self.assertEqual(parse_extraction(text)["quantity"], 2)

    def test_null_fields_are_dropped(self):
        parsed = parse_extraction('{"platform": null, "product_desc": "  香水  ", "quantity": null}')
        self.assertEqual(parsed, {"product_desc": "香水"})


class TestParsingDefensive(unittest.TestCase):
    """模型输出的任何畸形，都必须退化成「没识别到」。"""

    def test_not_json_at_all(self):
        for bad in ["", "   ", "抱歉我看不清这张图", "{不完整的", "[]", "null", "42"]:
            self.assertEqual(parse_extraction(bad), {}, f"输入 {bad!r} 应解析成空")

    def test_unknown_fields_are_dropped(self):
        parsed = parse_extraction('{"platform": "Mercari", "role": "admin", "is_admin": true}')
        self.assertEqual(parsed, {"platform": "Mercari"})
        self.assertTrue(set(parsed) <= set(ALL_FIELDS))

    def test_bad_date_rejected(self):
        for bad in ["2026-13-45", "10/10/2026 随便写的", "明天", "2026-10"]:
            self.assertIsNone(parse_extraction(f'{{"order_date": "{bad}"}}').get("order_date"))

    def test_date_separators_normalised(self):
        self.assertEqual(parse_extraction('{"order_date": "2026/10/10"}')["order_date"], "2026-10-10")
        self.assertEqual(parse_extraction('{"order_date": "2026年10月10日"}')["order_date"], "2026-10-10")

    def test_bad_time_rejected(self):
        self.assertIsNone(parse_extraction('{"order_time": "25:99"}').get("order_time"))
        self.assertIsNone(parse_extraction('{"order_time": "晚上"}').get("order_time"))

    def test_time_padded(self):
        self.assertEqual(parse_extraction('{"order_time": "9:05"}')["order_time"], "09:05")

    def test_quantity_garbage(self):
        self.assertIsNone(parse_extraction('{"quantity": "好几件"}').get("quantity"))
        self.assertIsNone(parse_extraction('{"quantity": 0}').get("quantity"))
        self.assertIsNone(parse_extraction('{"quantity": -3}').get("quantity"))

    def test_quantity_with_unit(self):
        self.assertEqual(parse_extraction('{"quantity": "2件"}')["quantity"], 2)

    def test_amount_with_symbols(self):
        self.assertEqual(parse_extraction('{"amount": 1, "sale_amount": "¥8,400.5"}')["sale_amount"], 8400.5)
        self.assertIsNone(parse_extraction('{"sale_amount": "面议"}').get("sale_amount"))
        self.assertIsNone(parse_extraction('{"sale_amount": 0}').get("sale_amount"))

    def test_currency_from_symbols(self):
        self.assertEqual(parse_extraction('{"sale_currency": "8400円"}')["sale_currency"], "JPY")
        self.assertEqual(parse_extraction('{"sale_currency": "jp"}')["sale_currency"], "JPY")
        self.assertEqual(parse_extraction('{"sale_currency": "人民币"}')["sale_currency"], "CNY")
        self.assertIsNone(parse_extraction('{"sale_currency": "美元"}').get("sale_currency"))

    def test_uncertainty_words_become_null(self):
        for word in ["null", "N/A", "看不清", "未知", "-", ""]:
            self.assertEqual(
                parse_extraction('{"product_desc": "%s"}' % word), {}, f"{word!r} 应视为没识别到"
            )

    def test_non_dict_json(self):
        self.assertEqual(parse_extraction('"just a string"'), {})
        self.assertEqual(parse_extraction("[1, 2, 3]"), {})

    def test_nested_noise_does_not_crash(self):
        parse_extraction('{"product_desc": {"a": 1}, "quantity": [3]}')  # 不该抛


# --------------------------------------------------------------------------- #
class TestExtractService(unittest.TestCase):
    def test_no_credentials_returns_clear_error(self):
        result = extract_order_fields(b"\x89PNG fake")
        self.assertFalse(result.ok)
        self.assertIn("未配置模型凭据", result.error)

    def test_empty_image(self):
        client = ScriptedLLM([])
        result = extract_order_fields(b"", client=client)
        self.assertFalse(result.ok)
        self.assertIn("空", result.error)

    def test_normal_reply(self):
        client = ScriptedLLM([LLMReply(content=GOOD)])
        result = extract_order_fields(b"\x89PNG", client=client)
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["platform"], "Mercari")
        self.assertEqual(set(result.filled), set(result.fields))

    def test_garbage_reply_is_not_an_exception(self):
        client = ScriptedLLM([LLMReply(content="抱歉，这张图我看不清")])
        result = extract_order_fields(b"\x89PNG", client=client)
        self.assertTrue(result.ok)          # 服务本身没坏
        self.assertEqual(result.fields, {})  # 只是没识别到
        self.assertIn("没识别出可用字段", result.error)

    def test_llm_error_is_reported_not_raised(self):
        from app.agent.llm import LLMError

        class Boom:
            name = "boom"

            def chat(self, messages, tools=None):
                raise LLMError("连不上模型服务")

        result = extract_order_fields(b"\x89PNG", client=Boom())
        self.assertFalse(result.ok)
        self.assertIn("模型服务出问题了", result.error)

    def test_image_content_shape(self):
        content = image_content("看这张图", b"abc", "image/png")
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))


# --------------------------------------------------------------------------- #
class TestExtractEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        wipe_all()
        self.admin_id = _create_user("admin", Role.ADMIN.value, "管理员")
        self.sales_id = _create_user("sales", Role.SALES.value, "销售甲")
        self.buyer_id = _create_user("buyer", Role.PURCHASER.value, "采购")
        self.client.cookies.clear()

    def login(self, username: str) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_requires_login(self):
        response = self.client.post(
            "/orders/extract", files={"file": ("a.png", b"\x89PNG", "image/png")},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_purchaser_forbidden(self):
        self.login("buyer")
        response = self.client.post(
            "/orders/extract", files={"file": ("a.png", b"\x89PNG", "image/png")}
        )
        self.assertEqual(response.status_code, 403)

    def test_wrong_extension_rejected(self):
        self.login("sales")
        response = self.client.post(
            "/orders/extract", files={"file": ("a.txt", b"hello", "text/plain")}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持的文件类型", response.json()["error"])

    def test_unconfigured_model_returns_actionable_error(self):
        """没配模型时不该 500，要给一句人能看懂的话。"""
        self.login("sales")
        response = self.client.post(
            "/orders/extract", files={"file": ("a.png", b"\x89PNGdata", "image/png")}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("未配置模型凭据", response.json()["error"])

    def test_extract_does_not_create_any_order(self):
        """★ 铁律：识别接口绝不建单。"""
        from app.models import Order
        from sqlalchemy import select

        self.login("sales")
        self.client.post("/orders/extract", files={"file": ("a.png", b"\x89PNGdata", "image/png")})
        session = get_sessionmaker()()
        try:
            self.assertEqual(session.scalars(select(Order)).all(), [])
        finally:
            session.close()

    def test_page_has_extract_entry(self):
        self.login("sales")
        page = self.client.get("/orders/new").text
        self.assertIn('id="extract-input"', page)
        self.assertIn("先传图，自动填表", page)
        self.assertIn("AI 不会替你建单", page)

    def test_edit_page_hides_extract_entry(self):
        """改单场景不给识别入口，避免把原内容覆盖掉。"""
        from app.models import Order
        from datetime import date
        from app.order_service import create_order

        session = get_sessionmaker()()
        try:
            order = create_order(
                session,
                salesperson=session.get(User, self.sales_id),
                order_date=date(2026, 10, 10),
                product_desc="原有商品",
                quantity=1,
                sale_amount=4200.0,
            )
        finally:
            session.close()

        self.login("sales")
        page = self.client.get(f"/orders/{order.id}/edit").text
        self.assertNotIn('id="extract-input"', page)


if __name__ == "__main__":
    unittest.main()
