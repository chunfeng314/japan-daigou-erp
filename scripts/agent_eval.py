"""助手回答质量评测：拿一批"老板真会问的话"去打助手，打印回答供人判断。

    .venv/bin/python scripts/agent_eval.py              # 用临时库跑一遍
    .venv/bin/python scripts/agent_eval.py --role sales # 以销售身份问

这不是自动化测试（回答好不好没法用断言判），是一个**回答质量的回归基线**：
改了提示词或加了工具之后，再跑一遍对比，看是变好了还是变差了。

为什么要它：助手的"粗糙"是主观的，但**具体粗糙在哪儿**得看输出。
第一次跑就暴露出：缺工具（采购成本查不到）、不给优先级、不说"下一步该做什么"、
不同角色问同一个问题答得一样。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

_tmpdir = Path(tempfile.mkdtemp(prefix="erp-agent-eval-"))
import os  # noqa: E402

os.environ.setdefault("ERP_DB_PATH", str(_tmpdir / "eval.sqlite3"))
os.environ.setdefault("ERP_UPLOAD_DIR", str(_tmpdir / "uploads"))
os.environ.setdefault("ERP_SECRET_KEY", "eval-" + "e" * 40)

from sqlalchemy import select  # noqa: E402

from app.agent import ask, is_configured  # noqa: E402
from app.db import get_sessionmaker, init_db, reset_state  # noqa: E402
from app.models import User  # noqa: E402
from app.order_service import create_order  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.state_machine import Role  # noqa: E402
from app.timeutil import utcnow_naive  # noqa: E402

reset_state()
init_db()

TODAY = date(2026, 10, 10)


# --------------------------------------------------------------------------- #
def seed() -> dict[str, int]:
    """造一批贴近真实的订单：各种状态、有的卡住、有的有异常、有的有采购成本。"""
    session = get_sessionmaker()()
    try:
        users = {}
        for username, display, role in [
            ("boss", "老板", Role.ADMIN.value),
            ("sales", "销售甲", Role.SALES.value),
            ("buyer", "采购一", Role.PURCHASER.value),
        ]:
            user = User(
                username=username, display_name=display, role=role,
                password_hash=hash_password("x"), is_active=1,
            )
            session.add(user)
            users[username] = user
        session.commit()

        sales = users["sales"]

        # (描述, 金额, 停留天数, 是否异常, 采购成本)
        plan = [
            ("【限定】一番搾り 橙汁 350ml", 9800, 0, False, None),      # 待采购 新单
            ("Bally 运动鞋 37码 复古款", 12800, 0, False, None),        # 待采购 + 少货异常
            ("TAMBURINS 化妆包 三件套", 5600, 6, False, 128.0),        # 采购中（已超 3 天）
            ("星野源 亚克力立牌 亚克力", 4200, 9, False, 96.0),         # 待上海收货（超 7 天）
            ("无印良品 化妆棉 200枚", 3200, 2, False, 45.0),           # 待上海收货 正常
            ("川口叶奈 写真集 限定版", 22800, 11, False, 380.0),       # 已收货待安排带货（超 7 天）
            ("MUJi 袖珍香薰机", 7800, 1, False, 210.0),                # 已安排带货 正常
            ("Moleskine 周计划本", 6900, 16, False, 88.0),             # 已发货（超 14 天）
            ("无印良品 水乳套装", 15800, 2, False, 320.0),             # 待结算
            ("Harajuku 辣妹风 假发", 8800, 0, False, None),            # 待采购 新单
            ("【绝版】 collectors 手办 1/72", 36000, 0, False, None),  # 待采购 新单 高价
            ("丝芙兰 眼影盘 十二色", 6600, 0, True, None),            # 待采购 + 错货异常
        ]

        created = []
        for index, (desc, amount, days, has_ex, cost) in enumerate(plan, start=1):
            order = create_order(
                session, salesperson=sales, order_date=TODAY - timedelta(days=index),
                product_desc=desc, quantity=1, sale_amount=float(amount),
            )
            # 推进到目标状态（直接改库，只为造数据；生产链路由状态机管）
            status = "待采购"
            if cost is not None:
                session.add(_purchase(order, users["buyer"], cost))
                status = "采购中" if days == 6 else _next_of(cost, days)
            order.status = status
            if has_ex:
                order.has_exception = 1
                session.add(_exception(order, has_ex))
            created.append(order)

        # 造流转记录并把时间往前拨
        from app.models import StatusLog

        for order in created:
            session.add(StatusLog(
                order_id=order.id, from_status=None, to_status=order.status,
                operator_id=users["boss"].id, note="造数据",
            ))
        session.commit()

        for order, (_, _, days, _, _) in zip(created, plan):
            log = session.scalars(
                select(StatusLog).where(StatusLog.order_id == order.id)
                .order_by(StatusLog.id.desc())
            ).first()
            if log is not None:
                log.created_at = utcnow_naive() - timedelta(days=days)
        session.commit()

        return {name: user.id for name, user in users.items()}
    finally:
        session.close()


def _next_of(cost: float, days: int) -> str:
    if cost and days == 2 and cost == 45.0:
        return "待上海收货"
    if cost and days == 11:
        return "已收货待安排带货"
    if cost and days == 1:
        return "已安排带货"
    if cost and days == 16:
        return "已发货"
    return "采购中"


def _purchase(order, buyer, cost: float):
    from app.models import Purchase

    return Purchase(
        order_id=order.id, purchaser_id=buyer.id,
        purchase_price=cost, purchase_currency="CNY",
        source_platform="淘宝", domestic_tracking_no="SF%010d" % order.id,
        carrier="顺丰",
    )


def _exception(order, mismatch: bool):
    from app.models import OrderException

    return OrderException(
        order_id=order.id,
        exception_type="错货" if mismatch else "少货",
        found_stage="已发货", qty_affected=1,
        detail="买家反馈收到的商品与订单不符" if mismatch else "买家反馈少了一件",
        ex_status="待处理",
    )


# --------------------------------------------------------------------------- #
QUERIES = [
    ("boss", "现在有什么需要我操心的"),
    ("boss", "我该先处理哪一单"),
    ("boss", "哪些单卡住了"),
    ("boss", "采购成本一共花了多少钱"),
    ("boss", "最近卖得怎么样"),
    ("boss", "有没有异常还没处理"),
    ("boss", "待上海收货的有几单"),
    ("sales", "我的进行中的单有哪些"),
    ("sales", "有没有买家催的单"),
    ("buyer", "待采购的有几单"),
    ("buyer", "有哪些单我该跟进了"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="助手回答质量评测")
    parser.add_argument("--only", default="", help="只跑包含该关键字的问题")
    args = parser.parse_args()

    ids = seed()
    print(f"已造 12 张单（覆盖待采购/采购中/待收货/带货/已发货/待结算 + 异常 + 超时）")
    print(f"模型：{'已配置 ' + __import__('app.config', fromlist=['x']).LLM_MODEL if is_configured() else '未配置（规则模式）'}")
    print("=" * 78)

    session = get_sessionmaker()()
    try:
        for username, question in QUERIES:
            if args.only and args.only not in question:
                continue
            user = session.get(User, ids[username])
            answer = ask(session, user, question)
            tools = "、".join(t.name for t in answer.tools) or "（未调用工具）"
            print(f"\n【{user.display_name}】{question}")
            print(f"  工具：{tools}")
            print(f"  引用：{list(answer.citations) or '无'}")
            print("  ── 回答 " + "─" * 60)
            for line in answer.answer.splitlines():
                print(f"  {line}")
    finally:
        session.close()

    shutil.rmtree(_tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
