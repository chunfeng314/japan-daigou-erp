"""状态推进与异常追踪测试（D4~D6）。

⚠️ 与 test_orders 一样：显式改写 config.DB_PATH 再 reset，避免同进程里
先导入的测试模块把数据库路径"抢跑"了。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import config
from app.db import get_sessionmaker, init_db, reset_state
from app.storage import reset_storage

_TMP_DIR = Path(tempfile.mkdtemp(prefix="erp-stage-test-"))
config.DB_PATH = _TMP_DIR / "stages.sqlite3"
config.UPLOAD_DIR = _TMP_DIR / "uploads"
config.SECRET_KEY = "test-secret-key-not-for-production"

reset_state()
init_db()

from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    JpDelivery,
    JpShipment,
    Order,
    OrderException,
    Purchase,
    Settlement,
    ShReceive,
    StatusLog,
    User,
)
from app.order_service import create_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import ExceptionStatus, Role, Status  # noqa: E402
from app.transition_service import advance_order, options_for, stage_fields  # noqa: E402
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


class StageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_storage()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        wipe_all()

        self.admin_id = _create_user("admin", Role.ADMIN.value, "管理员")
        self.sales_id = _create_user("sales", Role.SALES.value, "销售甲")
        self.other_sales_id = _create_user("sales2", Role.SALES.value, "销售乙")
        self.buyer_id = _create_user("buyer", Role.PURCHASER.value, "采购")
        self.wh_id = _create_user("wh", Role.WAREHOUSE.value, "仓管")
        self.client.cookies.clear()

    # ---------- 工具 ----------
    def login(self, username: str):
        response = self.client.post(
            "/login",
            data={"username": username, "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)

    def make_order(self, at: Status = Status.PENDING_PURCHASE, sales_id: int | None = None) -> Order:
        session = _session()
        try:
            owner = session.get(User, sales_id or self.sales_id)
            order = create_order(
                session,
                salesperson=owner,
                order_date=date(2026, 10, 10),
                product_desc="限定 香水 100ml",
                quantity=1,
                sale_amount=1000.0,
            )
            order.status = at.value
            session.commit()
            session.refresh(order)
            return order
        finally:
            session.close()

    def advance(self, order_id: int, to: Status, username: str, **data):
        """走 HTTP 端点推进，贴近真实调用路径。"""
        self.client.cookies.clear()
        self.login(username)
        payload = {"to_status": to.value}
        payload.update({k: str(v) for k, v in data.items()})
        return self.client.post(
            f"/orders/{order_id}/transition", data=payload, follow_redirects=False
        )

    def reload(self, order_id: int) -> Order:
        session = _session()
        try:
            return session.get(Order, order_id)
        finally:
            session.close()

    def assert_rejected(self, response):
        """被拒有两种形态，都算数：
        - 可见性守卫先拦下 → 403（看不到的单，压根不该出现操作按钮）
        - 可见但状态机不允许 → 303 带 error 提示
        """
        if response.status_code == 403:
            return
        self.assertEqual(response.status_code, 303, response.text[:300])
        self.assertIn("error=", response.headers.get("location", ""))

    def advance_ok(self, order_id: int, to: Status, username: str, **data) -> None:
        response = self.advance(order_id, to, username, **data)
        self.assertIn(
            "msg=", response.headers.get("location", ""),
            f"{to.value} 应被允许，实际：{response.status_code} {response.text[:200]}",
        )


# --------------------------------------------------------------------------- #
class TestFullyHappyPath(StageTestCase):
    def test_end_to_end_chain(self):
        order = self.make_order()
        oid = order.id

        r = self.advance(oid, Status.PURCHASING, "buyer")
        self.assertEqual(r.status_code, 303)
        self.assertIn("msg=", r.headers["location"])
        self.assertEqual(self.reload(oid).status, Status.PURCHASING.value)

        r = self.advance(
            oid,
            Status.WAITING_SH_RECEIPT,
            "buyer",
            purchase_price="128.5",
            purchase_currency="CNY",
            source_platform="淘宝",
            domestic_tracking_no="SF1234567890",
            carrier="顺丰",
        )
        self.assertIn("msg=", r.headers["location"])
        self.assertEqual(self.reload(oid).status, Status.WAITING_SH_RECEIPT.value)

        r = self.advance(
            oid,
            Status.RECEIVED_AWAITING_HANDCARRY,
            "wh",
            received_date="2026-10-12",
            handler="王五",
        )
        self.assertIn("msg=", r.headers["location"])

        r = self.advance(
            oid,
            Status.HANDCARRY_ARRANGED,
            "wh",
            carrier_person="张三",
            flight_date="2026-10-15",
            flight_no="MU575",
        )
        self.assertIn("msg=", r.headers["location"])

        r = self.advance(oid, Status.SHIPPED, "wh", ship_date="2026-10-18")
        self.assertIn("msg=", r.headers["location"])

        r = self.advance(oid, Status.WAITING_SETTLEMENT, "sales")
        self.assertIn("msg=", r.headers["location"])

        r = self.advance(
            oid, Status.COMPLETED, "sales", settle_date="2026-10-20", final_amount="1050"
        )
        self.assertIn("msg=", r.headers["location"])
        self.assertEqual(self.reload(oid).status, Status.COMPLETED.value)

        # 六个环节的数据都落到位了
        session = _session()
        try:
            purchase = session.scalar(select(Purchase).where(Purchase.order_id == oid))
            receive = session.scalar(select(ShReceive).where(ShReceive.order_id == oid))
            shipment = session.scalar(select(JpShipment).where(JpShipment.order_id == oid))
            delivery = session.scalar(select(JpDelivery).where(JpDelivery.order_id == oid))
            settlement = session.scalar(select(Settlement).where(Settlement.order_id == oid))

            self.assertEqual(purchase.purchase_price, 128.5)
            self.assertEqual(purchase.domestic_tracking_no, "SF1234567890")
            self.assertEqual(purchase.purchase_status, "已完成")
            self.assertEqual(receive.handler, "王五")
            self.assertEqual(shipment.carrier_person, "张三")
            self.assertEqual(str(delivery.ship_date), "2026-10-18")
            self.assertEqual(settlement.final_amount, 1050.0)

            # 流转记录：录单 1 条 + 推进 7 次 = 8 条
            logs = session.scalars(
                select(StatusLog).where(StatusLog.order_id == oid).order_by(StatusLog.id)
            ).all()
            self.assertEqual(len(logs), 8)
            self.assertEqual(logs[0].to_status, Status.PENDING_PURCHASE.value)
            self.assertEqual(logs[-1].to_status, Status.COMPLETED.value)
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestStagePersistence(StageTestCase):
    def test_purchase_start_marks_purchaser(self):
        order = self.make_order()
        self.advance(order.id, Status.PURCHASING, "buyer")
        session = _session()
        try:
            row = session.scalar(select(Purchase).where(Purchase.order_id == order.id))
            self.assertEqual(row.purchaser_id, self.buyer_id)
            self.assertEqual(row.purchase_status, "采购中")
            self.assertIsNone(row.purchase_price)
        finally:
            session.close()

    def test_receive_note_saved(self):
        order = self.make_order(Status.WAITING_SH_RECEIPT)
        self.advance(
            order.id,
            Status.RECEIVED_AWAITING_HANDCARRY,
            "wh",
            received_date="2026-10-12",
            handler="王五",
            note="外箱有点压痕，内件完好",
        )
        session = _session()
        try:
            row = session.scalar(select(ShReceive).where(ShReceive.order_id == order.id))
            self.assertEqual(row.received, 1)
            self.assertEqual(row.note, "外箱有点压痕，内件完好")
        finally:
            session.close()

    def test_delivery_records_operator(self):
        order = self.make_order(Status.HANDCARRY_ARRANGED)
        self.advance(order.id, Status.SHIPPED, "wh", ship_date="2026-10-18", tracking_no="JP999")
        session = _session()
        try:
            row = session.scalar(select(JpDelivery).where(JpDelivery.order_id == order.id))
            self.assertEqual(row.recorded_by, self.wh_id)
            self.assertEqual(row.tracking_no, "JP999")
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestPermissionOnStages(StageTestCase):
    def test_sales_cannot_do_purchasing(self):
        order = self.make_order()
        response = self.advance(order.id, Status.PURCHASING, "sales")
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.PENDING_PURCHASE.value)

    def test_purchaser_cannot_confirm_receipt(self):
        # 先把单推到「待上海收货」，这样采购才看得见这单（有 purchases 记录）
        order = self.make_order()
        self.advance_ok(order.id, Status.PURCHASING, "buyer")
        self.advance_ok(
            order.id, Status.WAITING_SH_RECEIPT, "buyer",
            purchase_price="100", domestic_tracking_no="SF1",
        )

        response = self.advance(
            order.id, Status.RECEIVED_AWAITING_HANDCARRY, "buyer",
            received_date="2026-10-12", handler="王五",
        )
        self.assert_rejected(response)
        self.assertEqual(self.reload(order.id).status, Status.WAITING_SH_RECEIPT.value)

    def test_warehouse_cannot_register_purchase(self):
        order = self.make_order()
        response = self.advance(order.id, Status.PURCHASING, "wh")
        self.assertIn("error=", response.headers["location"])

    def test_other_sales_cannot_settle(self):
        order = self.make_order(Status.WAITING_SETTLEMENT, sales_id=self.sales_id)
        response = self.advance(
            order.id, Status.COMPLETED, "sales2", settle_date="2026-10-20", final_amount="1000"
        )
        self.assert_rejected(response)
        self.assertEqual(self.reload(order.id).status, Status.WAITING_SETTLEMENT.value)

    def test_admin_can_do_any_stage(self):
        order = self.make_order()
        response = self.advance(order.id, Status.PURCHASING, "admin")
        self.assertIn("msg=", response.headers["location"])

    def test_unknown_target_status_rejected(self):
        order = self.make_order()
        self.login("admin")
        response = self.client.post(
            f"/orders/{order.id}/transition",
            data={"to_status": "不存在的状态"},
            follow_redirects=False,
        )
        self.assertIn("error=", response.headers["location"])

    def test_missing_required_field_rejected(self):
        # 走正常路径进到「采购中」（顺带建出 purchases 记录，采购才看得见这单）
        order = self.make_order()
        self.advance_ok(order.id, Status.PURCHASING, "buyer")

        response = self.advance(
            order.id, Status.WAITING_SH_RECEIPT, "buyer", purchase_price="100"
        )
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.PURCHASING.value)


# --------------------------------------------------------------------------- #
class TestSettlementRules(StageTestCase):
    def test_settle_date_required(self):
        order = self.make_order(Status.WAITING_SETTLEMENT, sales_id=self.sales_id)
        response = self.advance(order.id, Status.COMPLETED, "sales", final_amount="1000")
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.WAITING_SETTLEMENT.value)

    def test_small_gap_needs_no_confirmation(self):
        order = self.make_order(Status.WAITING_SETTLEMENT, sales_id=self.sales_id)
        response = self.advance(
            order.id, Status.COMPLETED, "sales", settle_date="2026-10-20", final_amount="1100"
        )
        self.assertIn("msg=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.COMPLETED.value)

    def test_large_gap_requires_confirmation(self):
        order = self.make_order(Status.WAITING_SETTLEMENT, sales_id=self.sales_id)
        response = self.advance(
            order.id, Status.COMPLETED, "sales", settle_date="2026-10-20", final_amount="500"
        )
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.WAITING_SETTLEMENT.value)

    def test_large_gap_passes_after_confirmation(self):
        order = self.make_order(Status.WAITING_SETTLEMENT, sales_id=self.sales_id)
        response = self.advance(
            order.id, Status.COMPLETED, "sales",
            settle_date="2026-10-20", final_amount="500", confirm_mismatch="1",
        )
        self.assertIn("msg=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.COMPLETED.value)


# --------------------------------------------------------------------------- #
class TestCancelAndRollback(StageTestCase):
    def test_cancel_requires_reason(self):
        order = self.make_order()
        self.login("admin")
        response = self.client.post(
            f"/orders/{order.id}/transition",
            data={"to_status": Status.CANCELLED.value},
            follow_redirects=False,
        )
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.PENDING_PURCHASE.value)

    def test_admin_cancels_with_reason(self):
        order = self.make_order()
        response = self.advance(order.id, Status.CANCELLED, "admin", cancel_reason="买家取消")
        self.assertIn("msg=", response.headers["location"])
        reloaded = self.reload(order.id)
        self.assertEqual(reloaded.status, Status.CANCELLED.value)
        self.assertEqual(reloaded.cancel_reason, "买家取消")

    def test_owner_sales_can_cancel_own(self):
        order = self.make_order()
        response = self.advance(order.id, Status.CANCELLED, "sales", cancel_reason="拍错")
        self.assertIn("msg=", response.headers["location"])

    def test_other_sales_cannot_cancel(self):
        order = self.make_order()
        response = self.advance(order.id, Status.CANCELLED, "sales2", cancel_reason="我的")
        self.assert_rejected(response)
        self.assertEqual(self.reload(order.id).status, Status.PENDING_PURCHASE.value)

    def test_purchaser_cannot_cancel(self):
        order = self.make_order()
        response = self.advance(order.id, Status.CANCELLED, "buyer", cancel_reason="x")
        self.assertIn("error=", response.headers["location"])

    def test_admin_rollback_from_completed(self):
        order = self.make_order(Status.COMPLETED)
        response = self.advance(
            order.id, Status.WAITING_SETTLEMENT, "admin", rollback_reason="结算金额录错"
        )
        self.assertIn("msg=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.WAITING_SETTLEMENT.value)

    def test_sales_cannot_rollback(self):
        order = self.make_order(Status.COMPLETED)
        response = self.advance(
            order.id, Status.WAITING_SETTLEMENT, "sales", rollback_reason="改一下"
        )
        self.assertIn("error=", response.headers["location"])


# --------------------------------------------------------------------------- #
class TestOptionsForRole(StageTestCase):
    def _dst(self, order: Order, user_id: int) -> set[str]:
        session = _session()
        try:
            user = session.get(User, user_id)
            return {o["dst"] for o in options_for(session, user, order)}
        finally:
            session.close()

    def test_purchaser_options(self):
        order = self.make_order()
        self.assertEqual(self._dst(order, self.buyer_id), {Status.PURCHASING.value})

    def test_sales_owner_options_at_start(self):
        order = self.make_order()
        # 待采购这一步销售推不动，但能取消自己的单
        self.assertEqual(self._dst(order, self.sales_id), {Status.CANCELLED.value})

    def test_warehouse_options(self):
        order = self.make_order(Status.WAITING_SH_RECEIPT)
        self.assertEqual(
            self._dst(order, self.wh_id), {Status.RECEIVED_AWAITING_HANDCARRY.value}
        )

    def test_admin_sees_forward_and_cancel(self):
        order = self.make_order()
        self.assertEqual(
            self._dst(order, self.admin_id),
            {Status.PURCHASING.value, Status.CANCELLED.value},
        )

    def test_admin_sees_rollback_from_terminal(self):
        order = self.make_order(Status.COMPLETED)
        self.assertEqual(
            self._dst(order, self.admin_id), {Status.WAITING_SETTLEMENT.value}
        )

    def test_stage_fields_match_required_from_state_machine(self):
        """表单上的 required 星号必须与状态机声明的必填一致，防止两处漂移。"""
        order = self.make_order(Status.PURCHASING)
        session = _session()
        try:
            user = session.get(User, self.buyer_id)
            option = options_for(session, user, order)[0]
            flagged = {f.name for f in option["fields"] if f.required}
            self.assertEqual(flagged, {"purchase_price", "domestic_tracking_no"})
        finally:
            session.close()

    def test_every_transition_has_field_spec(self):
        """每个合法流转都要有字段声明（可为空），否则详情页渲染不出表单。"""
        from app.state_machine import TRANSITIONS, TERMINAL_STATUSES

        for transition in TRANSITIONS:
            fields = stage_fields(transition.src, transition.dst)
            names = {f.name for f in fields}
            for required in transition.required_fields:
                self.assertIn(
                    required, names,
                    f"{transition.src.value}→{transition.dst.value} 的必填 {required} 没有对应表单字段",
                )
        for status in TERMINAL_STATUSES:
            self.assertTrue(stage_fields(status, Status.PENDING_PURCHASE))


# --------------------------------------------------------------------------- #
class TestExceptions(StageTestCase):
    def _detail(self, order_id: int, username: str) -> str:
        self.client.cookies.clear()
        self.login(username)
        return self.client.get(f"/orders/{order_id}").text

    def test_register_exception_sets_flag(self):
        order = self.make_order()
        self.login("wh")
        response = self.client.post(
            f"/orders/{order.id}/exceptions",
            data={
                "exception_type": "少货",
                "found_stage": "收货",
                "qty_affected": "1",
                "detail": "少了一件",
            },
            follow_redirects=False,
        )
        self.assertIn("msg=", response.headers["location"])

        session = _session()
        try:
            row = session.scalars(select(OrderException)).one()
            self.assertEqual(row.exception_type, "少货")
            self.assertEqual(row.ex_status, ExceptionStatus.PENDING.value)
            self.assertEqual(session.get(Order, order.id).has_exception, 1)
        finally:
            session.close()

    def test_bad_exception_type_rejected(self):
        order = self.make_order()
        self.login("wh")
        response = self.client.post(
            f"/orders/{order.id}/exceptions",
            data={"exception_type": "外星人劫走"},
            follow_redirects=False,
        )
        self.assertIn("error=", response.headers["location"])

    def test_exception_does_not_change_status(self):
        order = self.make_order(Status.HANDCARRY_ARRANGED)
        self.login("sales")
        self.client.post(
            f"/orders/{order.id}/exceptions",
            data={"exception_type": "错货", "detail": "颜色不对"},
            follow_redirects=False,
        )
        self.assertEqual(self.reload(order.id).status, Status.HANDCARRY_ARRANGED.value)

    def test_order_still_advances_with_open_exception(self):
        """异常不阻塞主状态（暂定口径）。"""
        order = self.make_order(Status.HANDCARRY_ARRANGED)
        self.login("sales")
        self.client.post(
            f"/orders/{order.id}/exceptions",
            data={"exception_type": "少货", "detail": "少 1 件"},
            follow_redirects=False,
        )
        response = self.advance(order.id, Status.SHIPPED, "wh", ship_date="2026-10-18")
        self.assertIn("msg=", response.headers["location"])
        self.assertEqual(self.reload(order.id).status, Status.SHIPPED.value)

    def test_admin_resolves_and_flag_clears(self):
        order = self.make_order()
        self.login("sales")
        self.client.post(
            f"/orders/{order.id}/exceptions",
            data={"exception_type": "少货", "detail": "少 1 件"},
            follow_redirects=False,
        )
        session = _session()
        try:
            exception_id = session.scalars(select(OrderException)).one().id
        finally:
            session.close()

        self.client.cookies.clear()
        self.login("admin")
        response = self.client.post(
            f"/orders/{order.id}/exceptions/{exception_id}/resolve",
            data={"resolution": "退款"},
            follow_redirects=False,
        )
        self.assertIn("msg=", response.headers["location"])

        session = _session()
        try:
            row = session.get(OrderException, exception_id)
            self.assertEqual(row.ex_status, ExceptionStatus.RESOLVED.value)
            self.assertEqual(row.resolution, "退款")
            self.assertIsNotNone(row.resolved_at)
            self.assertEqual(session.get(Order, order.id).has_exception, 0)
        finally:
            session.close()

    def test_non_admin_cannot_resolve(self):
        order = self.make_order()
        self.login("wh")
        self.client.post(
            f"/orders/{order.id}/exceptions",
            data={"exception_type": "少货"},
            follow_redirects=False,
        )
        session = _session()
        try:
            exception_id = session.scalars(select(OrderException)).one().id
        finally:
            session.close()

        response = self.client.post(
            f"/orders/{order.id}/exceptions/{exception_id}/resolve",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

    def test_detail_page_shows_exception_and_badge(self):
        order = self.make_order()
        self.login("sales")
        self.client.post(
            f"/orders/{order.id}/exceptions",
            data={"exception_type": "破损", "detail": "外盒压坏"},
            follow_redirects=False,
        )

        detail = self._detail(order.id, "sales")
        self.assertIn("破损", detail)
        self.assertIn("外盒压坏", detail)
        self.assertIn("有异常", detail)

        list_page = self.client.get("/orders").text
        self.assertIn("异常", list_page)

    def test_admin_can_reopen(self):
        order = self.make_order()
        session = _session()
        try:
            order.has_exception = 1
            row = OrderException(
                order_id=order.id, exception_type="少货", ex_status=ExceptionStatus.RESOLVED.value
            )
            session.add(row)
            session.commit()
            exception_id = row.id
        finally:
            session.close()

        self.login("admin")
        response = self.client.post(
            f"/orders/{order.id}/exceptions/{exception_id}/reopen", follow_redirects=False
        )
        self.assertIn("msg=", response.headers["location"])
        session = _session()
        try:
            self.assertEqual(
                session.get(OrderException, exception_id).ex_status, ExceptionStatus.PENDING.value
            )
            self.assertEqual(session.get(Order, order.id).has_exception, 1)
        finally:
            session.close()


# --------------------------------------------------------------------------- #
class TestAdvanceServiceDirectly(StageTestCase):
    def test_service_rejects_wrong_role(self):
        session = _session()
        try:
            order = self.make_order()
            sales = session.get(User, self.sales_id)
            result = advance_order(
                session, order=order, to_status=Status.PURCHASING, operator=sales, data={}
            )
            self.assertFalse(result.ok)
            self.assertIn("采购", result.message)
        finally:
            session.close()

    def test_service_reports_missing_fields(self):
        session = _session()
        try:
            order = self.make_order(Status.PURCHASING)
            buyer = session.get(User, self.buyer_id)
            result = advance_order(
                session,
                order=order,
                to_status=Status.WAITING_SH_RECEIPT,
                operator=buyer,
                data={"purchase_price": "10"},
            )
            self.assertFalse(result.ok)
            self.assertIn("国内快递单号", result.message)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
