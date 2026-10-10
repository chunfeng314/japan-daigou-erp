"""登录 / 会话 / 角色权限 集成测试。

⚠️ 必须在导入 app 之前设好环境变量 —— 配置是在模块导入时读取的。
数据库指向临时目录，不碰真实数据。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-test-"))
os.environ["ERP_DB_PATH"] = str(_TMP_DIR / "test.sqlite3")
os.environ["ERP_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["ERP_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.db import get_sessionmaker, init_db, reset_state  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role  # noqa: E402

PASSWORD = "pw123456"


def _create_user(
    username: str,
    role: str,
    password: str = PASSWORD,
    display_name: str | None = None,
    is_active: int = 1,
) -> int:
    session = get_sessionmaker()()
    try:
        user = User(
            username=username,
            display_name=display_name or username,
            role=role,
            password_hash=hash_password(password),
            is_active=is_active,
        )
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_state()
        init_db()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        session = get_sessionmaker()()
        try:
            # 只清理本模块自己造的用户，别 `delete(User)` 全表 —— 同进程里别的测试模块
            # 可能已经建了订单，全删会踩外键约束
            session.execute(
                delete(User).where(
                    User.username.in_(["admin", "sales1", "off1", "buyer1", "shortpw"])
                )
            )
            session.commit()
        finally:
            session.close()
        self.admin_id = _create_user("admin", Role.ADMIN.value, display_name="管理员甲")
        self.sales_id = _create_user("sales1", Role.SALES.value, display_name="销售乙")
        _create_user("off1", Role.WAREHOUSE.value, display_name="停用丙", is_active=0)
        self.client.cookies.clear()

    def login(self, username: str, password: str = PASSWORD):
        return self.client.post(
            "/login",
            data={"username": username, "password": password, "next": "/"},
            follow_redirects=False,
        )

    # ---------- 未登录 ----------
    def test_root_redirects_to_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_garbage_cookie_is_ignored(self):
        self.client.cookies.set("erp_session", "not-a-valid-token")
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    # ---------- 登录 ----------
    def test_login_page_renders(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("登录", response.text)

    def test_wrong_password_rejected(self):
        response = self.login("admin", "wrong-password")
        self.assertEqual(response.status_code, 400)
        self.assertIn("账号或密码不正确", response.text)

    def test_unknown_user_rejected_with_same_message(self):
        response = self.login("nobody")
        self.assertEqual(response.status_code, 400)
        self.assertIn("账号或密码不正确", response.text)

    def test_successful_login_reaches_home(self):
        response = self.login("admin")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")

        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("管理员甲", home.text)
        self.assertIn("管理员", home.text)

    def test_inactive_user_cannot_login(self):
        response = self.login("off1")
        self.assertEqual(response.status_code, 400)
        self.assertIn("账号或密码不正确", response.text)

    def test_already_logged_in_login_page_redirects(self):
        self.login("admin")
        response = self.client.get("/login", follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_open_redirect_is_blocked(self):
        """next 只允许站内相对路径。"""
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "next": "//evil.example.com"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")

    # ---------- 登出 ----------
    def test_logout_clears_session(self):
        self.login("admin")
        self.assertEqual(self.client.get("/").status_code, 200)

        self.client.post("/logout", follow_redirects=False)
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    # ---------- 权限 ----------
    def test_non_admin_cannot_open_account_management(self):
        self.login("sales1")
        response = self.client.get("/admin/users")
        self.assertEqual(response.status_code, 403)
        self.assertIn("无权访问", response.text)

    def test_admin_can_open_account_management(self):
        self.login("admin")
        response = self.client.get("/admin/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("账号管理", response.text)

    def test_anonymous_cannot_open_account_management(self):
        response = self.client.get("/admin/users", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    # ---------- 账号管理 ----------
    def test_admin_creates_user_who_can_then_log_in(self):
        self.login("admin")
        response = self.client.post(
            "/admin/users",
            data={
                "username": "buyer1",
                "display_name": "采购一",
                "role": Role.PURCHASER.value,
                "password": "pw654321",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        self.client.cookies.clear()
        login = self.login("buyer1", "pw654321")
        self.assertEqual(login.status_code, 303)
        home = self.client.get("/")
        self.assertIn("采购", home.text)

    def test_duplicate_username_rejected(self):
        self.login("admin")
        response = self.client.post(
            "/admin/users",
            data={
                "username": "sales1",
                "display_name": "重名",
                "role": Role.SALES.value,
                "password": "pw654321",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["location"])

    def test_short_password_rejected(self):
        self.login("admin")
        response = self.client.post(
            "/admin/users",
            data={
                "username": "shortpw",
                "display_name": "短口令",
                "role": Role.SALES.value,
                "password": "123",
            },
            follow_redirects=False,
        )
        self.assertIn("error=", response.headers["location"])

    def test_admin_cannot_disable_self(self):
        self.login("admin")
        response = self.client.post(
            f"/admin/users/{self.admin_id}/toggle", follow_redirects=False
        )
        self.assertIn("error=", response.headers["location"])
        session = get_sessionmaker()()
        try:
            admin = session.get(User, self.admin_id)
            self.assertEqual(admin.is_active, 1)
        finally:
            session.close()

    def test_disable_then_user_cannot_log_in(self):
        self.login("admin")
        self.client.post(f"/admin/users/{self.sales_id}/toggle", follow_redirects=False)

        self.client.cookies.clear()
        response = self.login("sales1")
        self.assertEqual(response.status_code, 400)

    def test_reset_password_takes_effect(self):
        self.login("admin")
        self.client.post(
            f"/admin/users/{self.sales_id}/reset-password",
            data={"new_password": "brand-new-pw"},
            follow_redirects=False,
        )
        self.client.cookies.clear()
        self.assertEqual(self.login("sales1", "brand-new-pw").status_code, 303)
        self.client.cookies.clear()
        self.assertEqual(self.login("sales1", PASSWORD).status_code, 400)

    def test_healthz(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.startswith("ok"))


class PasswordHashingTests(unittest.TestCase):
    def test_hash_is_salted(self):
        from app.security import hash_password, verify_password

        a = hash_password("same-password")
        b = hash_password("same-password")
        self.assertNotEqual(a, b)
        self.assertTrue(verify_password("same-password", a))
        self.assertTrue(verify_password("same-password", b))

    def test_wrong_password_fails(self):
        from app.security import hash_password, verify_password

        stored = hash_password("correct")
        self.assertFalse(verify_password("incorrect", stored))

    def test_malformed_hash_does_not_crash(self):
        from app.security import verify_password

        for bad in ["", "garbage", "md5$1$aa$bb", "pbkdf2_sha256$notanint$aa$bb"]:
            self.assertFalse(verify_password("x", bad))

    def test_session_token_roundtrip_and_tamper(self):
        from app.security import create_session_token, read_session_token

        token = create_session_token(42)
        self.assertEqual(read_session_token(token), 42)
        self.assertIsNone(read_session_token(token + "tampered"))
        self.assertIsNone(read_session_token("garbage"))


if __name__ == "__main__":
    unittest.main()
