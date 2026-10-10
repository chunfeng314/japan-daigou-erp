"""未配模型凭据时的**规则模式**。

开发文档 §9 自己写了「卡单检测 + 每日待办：**规则引擎即可**，LLM 润色」——
所以规则模式不是权宜之计，而是这类确定性问题的**正确解法**：
更快、不花额度、且**不可能胡说**。

它能覆盖的：查单进度 / 按状态列单 / 计数 / 我的待办 / 卡单。
覆盖不了的**明确说覆盖不了**，绝不猜。
"""

from __future__ import annotations

import re

from ..state_machine import Status
from .answer import AgentAnswer, ToolTrace
from .tools import AgentContext, run_tool

#: 订单号：20261010-001 / 20261010001 / 2026 1010-001
ORDER_NO_RE = re.compile(r"(\d{8})\s*[-－ ]?\s*(\d{3,})")

HELP_TEXT = """我是日代 ERP 的助手。现在没有配置模型凭据，走的是**规则模式**，只认这几类问法：

- `20261010-001 到哪一步了` —— 查单张订单的进度
- `我有哪些待办` —— 和你首页看到的一致
- `有哪些卡单` / `超时的单` —— 停留超期的订单
- `待上海收货的有几单` / `列出待结算的单` —— 按状态查单或计数

想看自由问答，请在 `.env` 里配上 `ERP_LLM_BASE_URL` / `ERP_LLM_API_KEY` / `ERP_LLM_MODEL`。"""


def _run(ctx: AgentContext, name: str, arguments: dict) -> AgentAnswer:
    result = run_tool(ctx, name, arguments)
    trace = ToolTrace(name=name, arguments=arguments, ok=result.ok, summary=result.summary)
    return AgentAnswer(
        answer=result.summary,
        citations=result.citations,
        tools=(trace,),
        mode="rules",
        error=result.error,
    )


def answer_with_rules(ctx: AgentContext, question: str) -> AgentAnswer:
    q = (question or "").strip()
    if not q:
        return AgentAnswer(HELP_TEXT, mode="rules")

    # 1) 订单号优先：问句里带单号，多半是查进度
    matched = ORDER_NO_RE.search(q)
    if matched:
        order_no = f"{matched.group(1)}-{matched.group(2)}"
        wants_progress = any(
            keyword in q for keyword in ("到哪", "进度", "什么情况", "怎么样", "状态", "在哪")
        )
        return _run(
            ctx,
            "order_progress" if wants_progress else "find_order",
            {"order_no": order_no},
        )

    # 2) 卡单
    if any(keyword in q for keyword in ("卡单", "超时", "滞留", "压了多久", "卡住")):
        return _run(ctx, "overdue_orders", {})

    # 3) 待办
    if any(keyword in q for keyword in ("待办", "该做", "要做什么", "我该", "该干什么")):
        return _run(ctx, "my_todos", {})

    wants_count = any(keyword in q for keyword in ("多少", "几单", "数量", "有几", "几个", "几笔"))

    # 4) 状态词（精确的状态名）
    for status in Status.values():
        if status in q:
            return _run(
                ctx,
                "count_orders" if wants_count else "list_orders",
                {"status": status},
            )

    # 5) 口语里的「未结算 / 没结算」→ 待结算
    if any(keyword in q for keyword in ("未结算", "没结算", "待结算", "还没结算")):
        return _run(
            ctx,
            "count_orders" if wants_count else "list_orders",
            {"status": Status.WAITING_SETTLEMENT.value},
        )

    # 6) 只有数字诉求
    if wants_count:
        return _run(ctx, "count_orders", {})

    return AgentAnswer(HELP_TEXT, mode="rules")
