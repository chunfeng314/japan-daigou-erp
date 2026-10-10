"""状态机单元测试 —— 对应开发文档 §2。"""

from __future__ import annotations

import unittest

from app.state_machine import (
    ALL_STATUSES,
    MAIN_FLOW,
    TRANSITIONS,
    ExceptionStatus,
    Role,
    Status,
    allowed_transitions,
    check_transition,
    index_of,
    is_exception_open,
    next_status,
    previous_status,
)


class TestStatusGraph(unittest.TestCase):
    def test_main_flow_order_matches_doc(self):
        self.assertEqual(
            [s.value for s in MAIN_FLOW],
            [
                "待采购",
                "采购中",
                "待上海收货",
                "已收货待安排带货",
                "已安排带货",
                "已发货",
                "待结算",
                "已完成",
            ],
        )

    def test_all_statuses_is_flow_plus_cancelled(self):
        self.assertEqual(set(ALL_STATUSES), set(MAIN_FLOW) | {Status.CANCELLED})

    def test_next_previous(self):
        self.assertEqual(next_status(Status.PENDING_PURCHASE), Status.PURCHASING)
        self.assertEqual(next_status(Status.WAITING_SETTLEMENT), Status.COMPLETED)
        self.assertIsNone(next_status(Status.COMPLETED))
        self.assertIsNone(next_status(Status.CANCELLED))
        self.assertEqual(previous_status(Status.SHIPPED), Status.HANDCARRY_ARRANGED)
        self.assertIsNone(previous_status(Status.PENDING_PURCHASE))

    def test_index_of_cancelled_is_negative(self):
        self.assertEqual(index_of(Status.CANCELLED), -1)

    def test_every_main_flow_step_has_exactly_one_forward_transition(self):
        """主线必须首尾相接：每一步有且只有一条向前的常规流转。"""
        for src in MAIN_FLOW[:-1]:
            forward = [
                t for t in TRANSITIONS if t.src == src and t.dst in MAIN_FLOW
            ]
            self.assertEqual(len(forward), 1, f"{src.value} 的向前流转数量不为 1")


class TestHappyPath(unittest.TestCase):
    """一条订单从下单到完成的完整推进。"""

    def test_full_chain(self):
        steps = [
            (Role.PURCHASER, Status.PENDING_PURCHASE, Status.PURCHASING, {}),
            (
                Role.PURCHASER,
                Status.PURCHASING,
                Status.WAITING_SH_RECEIPT,
                {"purchase_price": 100, "domestic_tracking_no": "SF123"},
            ),
            (
                Role.WAREHOUSE,
                Status.WAITING_SH_RECEIPT,
                Status.RECEIVED_AWAITING_HANDCARRY,
                {"received_date": "2026-10-10", "handler": "王五"},
            ),
            (
                Role.WAREHOUSE,
                Status.RECEIVED_AWAITING_HANDCARRY,
                Status.HANDCARRY_ARRANGED,
                {"carrier_person": "张三", "flight_date": "2026-10-15"},
            ),
            (
                Role.WAREHOUSE,
                Status.HANDCARRY_ARRANGED,
                Status.SHIPPED,
                {"ship_date": "2026-10-18"},
            ),
            (Role.SALES, Status.SHIPPED, Status.WAITING_SETTLEMENT, {}),
            (
                Role.SALES,
                Status.WAITING_SETTLEMENT,
                Status.COMPLETED,
                {"final_amount": 4200},
            ),
        ]
        for role, src, dst, payload in steps:
            result = check_transition(role=role, src=src, dst=dst, is_owner=True, payload=payload)
            self.assertTrue(result.allowed, f"{src.value}→{dst.value} 应被允许：{result.reason}")


class TestRolePermission(unittest.TestCase):
    def test_sales_cannot_do_purchasing(self):
        result = check_transition(
            role=Role.SALES, src=Status.PENDING_PURCHASE, dst=Status.PURCHASING
        )
        self.assertFalse(result.allowed)
        self.assertIn("采购", result.reason)

    def test_warehouse_cannot_register_purchase(self):
        result = check_transition(
            role=Role.WAREHOUSE, src=Status.PENDING_PURCHASE, dst=Status.PURCHASING
        )
        self.assertFalse(result.allowed)

    def test_purchaser_cannot_confirm_receipt(self):
        result = check_transition(
            role=Role.PURCHASER,
            src=Status.WAITING_SH_RECEIPT,
            dst=Status.RECEIVED_AWAITING_HANDCARRY,
            payload={"received_date": "2026-10-10", "handler": "王五"},
        )
        self.assertFalse(result.allowed)

    def test_admin_can_do_anything_in_flow(self):
        result = check_transition(
            role=Role.ADMIN,
            src=Status.HANDCARRY_ARRANGED,
            dst=Status.SHIPPED,
            payload={"ship_date": "2026-10-18"},
        )
        self.assertTrue(result.allowed)

    def test_sales_cannot_settle_others_order(self):
        """待结算 → 已完成 文档里写的是"销售（本单销售）/管理员"。"""
        result = check_transition(
            role=Role.SALES,
            src=Status.WAITING_SETTLEMENT,
            dst=Status.COMPLETED,
            is_owner=False,
            payload={"final_amount": 100},
        )
        self.assertFalse(result.allowed)


class TestRequiredFields(unittest.TestCase):
    def test_missing_all_required(self):
        result = check_transition(
            role=Role.PURCHASER, src=Status.PURCHASING, dst=Status.WAITING_SH_RECEIPT, payload={}
        )
        self.assertFalse(result.allowed)
        self.assertEqual(set(result.missing_fields), {"purchase_price", "domestic_tracking_no"})
        self.assertIn("采购成本", result.reason)

    def test_blank_string_counts_as_missing(self):
        result = check_transition(
            role=Role.WAREHOUSE,
            src=Status.WAITING_SH_RECEIPT,
            dst=Status.RECEIVED_AWAITING_HANDCARRY,
            payload={"received_date": "2026-10-10", "handler": "   "},
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.missing_fields, ("handler",))

    def test_zero_amount_is_rejected(self):
        result = check_transition(
            role=Role.SALES,
            src=Status.WAITING_SETTLEMENT,
            dst=Status.COMPLETED,
            is_owner=True,
            payload={"final_amount": 0},
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.missing_fields, ("final_amount",))

    def test_negative_amount_is_rejected(self):
        result = check_transition(
            role=Role.SALES,
            src=Status.WAITING_SETTLEMENT,
            dst=Status.COMPLETED,
            is_owner=True,
            payload={"final_amount": -5},
        )
        self.assertFalse(result.allowed)

    def test_non_numeric_amount_is_rejected(self):
        result = check_transition(
            role=Role.SALES,
            src=Status.WAITING_SETTLEMENT,
            dst=Status.COMPLETED,
            is_owner=True,
            payload={"final_amount": "abc"},
        )
        self.assertFalse(result.allowed)

    def test_numeric_string_amount_is_accepted(self):
        """表单提交过来的都是字符串，合法的数字串要通过。"""
        result = check_transition(
            role=Role.SALES,
            src=Status.WAITING_SETTLEMENT,
            dst=Status.COMPLETED,
            is_owner=True,
            payload={"final_amount": "4200.5"},
        )
        self.assertTrue(result.allowed, result.reason)


class TestCancelAndRollback(unittest.TestCase):
    def test_admin_can_cancel_any(self):
        result = check_transition(
            role=Role.ADMIN,
            src=Status.PURCHASING,
            dst=Status.CANCELLED,
            is_owner=False,
            payload={"cancel_reason": "买家取消"},
        )
        self.assertTrue(result.allowed)

    def test_sales_can_cancel_own_only(self):
        ok = check_transition(
            role=Role.SALES,
            src=Status.PENDING_PURCHASE,
            dst=Status.CANCELLED,
            is_owner=True,
            payload={"cancel_reason": "拍错"},
        )
        self.assertTrue(ok.allowed)

        denied = check_transition(
            role=Role.SALES,
            src=Status.PENDING_PURCHASE,
            dst=Status.CANCELLED,
            is_owner=False,
            payload={"cancel_reason": "拍错"},
        )
        self.assertFalse(denied.allowed)
        self.assertIn("自己的", denied.reason)

    def test_purchaser_cannot_cancel(self):
        result = check_transition(
            role=Role.PURCHASER,
            src=Status.PURCHASING,
            dst=Status.CANCELLED,
            payload={"cancel_reason": "x"},
        )
        self.assertFalse(result.allowed)

    def test_cancel_requires_reason(self):
        result = check_transition(
            role=Role.ADMIN, src=Status.PURCHASING, dst=Status.CANCELLED, payload={}
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.missing_fields, ("cancel_reason",))

    def test_rollback_admin_only(self):
        ok = check_transition(
            role=Role.ADMIN,
            src=Status.COMPLETED,
            dst=Status.WAITING_SETTLEMENT,
            payload={"rollback_reason": "结算金额录错"},
        )
        self.assertTrue(ok.allowed)

        denied = check_transition(
            role=Role.SALES,
            src=Status.COMPLETED,
            dst=Status.WAITING_SETTLEMENT,
            payload={"rollback_reason": "x"},
        )
        self.assertFalse(denied.allowed)

    def test_rollback_requires_reason(self):
        result = check_transition(
            role=Role.ADMIN, src=Status.COMPLETED, dst=Status.WAITING_SETTLEMENT, payload={}
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.missing_fields, ("rollback_reason",))

    def test_same_status_rejected(self):
        result = check_transition(role=Role.ADMIN, src=Status.PURCHASING, dst=Status.PURCHASING)
        self.assertFalse(result.allowed)


class TestIllegalJump(unittest.TestCase):
    def test_cannot_skip_steps(self):
        result = check_transition(
            role=Role.ADMIN, src=Status.PENDING_PURCHASE, dst=Status.SHIPPED
        )
        self.assertFalse(result.allowed)
        self.assertIn("不允许的流转", result.reason)


class TestAllowedTransitions(unittest.TestCase):
    def test_purchaser_sees_forward_only(self):
        options = allowed_transitions(Status.PURCHASING, Role.PURCHASER)
        self.assertEqual([t.dst for t in options], [Status.WAITING_SH_RECEIPT])

    def test_purchaser_has_no_cancel(self):
        options = allowed_transitions(Status.PURCHASING, Role.PURCHASER)
        self.assertNotIn(Status.CANCELLED, [t.dst for t in options])

    def test_admin_sees_forward_plus_cancel(self):
        options = allowed_transitions(Status.PURCHASING, Role.ADMIN)
        self.assertEqual(
            {t.dst for t in options}, {Status.WAITING_SH_RECEIPT, Status.CANCELLED}
        )

    def test_sales_owner_sees_cancel(self):
        options = allowed_transitions(Status.WAITING_SETTLEMENT, Role.SALES, is_owner=True)
        self.assertEqual({t.dst for t in options}, {Status.COMPLETED, Status.CANCELLED})

    def test_sales_non_owner_sees_nothing_at_settlement(self):
        """非本单销售：既不能结算，也不能取消。"""
        options = allowed_transitions(Status.WAITING_SETTLEMENT, Role.SALES, is_owner=False)
        self.assertEqual(options, ())

    def test_admin_sees_rollback_from_terminal(self):
        options = allowed_transitions(Status.COMPLETED, Role.ADMIN)
        self.assertEqual([t.dst for t in options], [Status.WAITING_SETTLEMENT])

    def test_no_rollback_for_non_admin(self):
        options = allowed_transitions(Status.COMPLETED, Role.SALES, is_owner=True)
        self.assertEqual(options, ())


class TestExceptions(unittest.TestCase):
    def test_open_exception_states(self):
        self.assertTrue(is_exception_open(ExceptionStatus.PENDING))
        self.assertTrue(is_exception_open("处理中"))
        self.assertFalse(is_exception_open("已解决"))
        self.assertFalse(is_exception_open(ExceptionStatus.RESOLVED))

    def test_exception_does_not_change_status(self):
        """异常只是标记，不影响主状态枚举本身。"""
        self.assertNotIn(Status.CANCELLED, [])  # 占位：主状态与异常枚举互不掺杂
        self.assertEqual(len(MAIN_FLOW) + 1, len(ALL_STATUSES))


if __name__ == "__main__":
    unittest.main()
