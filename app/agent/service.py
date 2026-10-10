"""Agent 编排：把「用户的一句话」变成「工具调用链 → 有依据的回答」。

流程（模型模式）：
    提问 → 模型决定调哪些工具 → **工具层校验权限并执行** → 结果回喂
         → 模型给最终回答（最多 `ERP_LLM_MAX_ROUNDS` 轮）

没配模型凭据时自动走规则模式（见 `rules.py`）。

安全边界：模型只能决定「调哪个工具、传什么参数」。工具层拿着**真实登录用户**
自己去查库，所以模型**无法**通过提示词看到别人的单——这条有测试盯着。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .. import config
from ..models import User
from .answer import AgentAnswer, ToolTrace
from .llm import LLMClient, LLMError, get_llm
from .rules import HELP_TEXT, answer_with_rules
from .tools import AgentContext, describe_tools, run_tool

SYSTEM_PROMPT = """你是「日代 ERP」的助手，服务这家中国代购发日本公司的内部员工。

硬性要求（违反即视为错误回答）：
1. 回答**只能基于工具返回的数据**。绝对不许猜测或编造订单号、金额、状态、日期。
2. 查不到就直说查不到，不要用常识补全，也不要说"可能是…"。
3. 你是**只读**的。你不能修改任何数据；需要推进状态时，告诉对方去订单详情页操作。
4. 金额照抄工具返回的数字，不要换算汇率、不要四舍五入、不要自己算利润。
5. 如果工具查不到某人问的单，如实告知"没查到你名下的这张单"，**不要尝试换参数绕过**。
6. 回答用中文，先说结论，简短；涉及具体订单时带上订单号与当前状态。
"""


def ask(
    db: Session,
    user: User,
    question: str,
    *,
    client: LLMClient | None = None,
) -> AgentAnswer:
    ctx = AgentContext(db=db, user=user)
    question = (question or "").strip()
    if not question:
        return AgentAnswer(HELP_TEXT, mode="rules" if client is None else "llm")

    active = client if client is not None else get_llm()
    if active is None:
        return answer_with_rules(ctx, question)
    return _llm_answer(ctx, active, question)


def _llm_answer(ctx: AgentContext, client: LLMClient, question: str) -> AgentAnswer:
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    traces: list[ToolTrace] = []
    citations: list[str] = []
    tool_schemas = describe_tools()

    for _ in range(max(1, config.LLM_MAX_ROUNDS)):
        try:
            reply = client.chat(messages, tool_schemas)
        except LLMError as exc:
            return AgentAnswer(
                f"模型服务出问题了，先看看下面的信息：{exc}",
                tools=tuple(traces),
                mode="llm",
                error="llm_error",
            )

        if not reply.wants_tools:
            content = (reply.content or "").strip()
            if not content:
                content = "模型没有给出回答，请换个问法。"
            return AgentAnswer(
                content,
                citations=tuple(dict.fromkeys(citations)),
                tools=tuple(traces),
                mode="llm",
            )

        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in reply.tool_calls
                ],
            }
        )

        for call in reply.tool_calls:
            # ★ 权限在工具层执行：这里传进去的是真实登录用户，模型改不了
            result = run_tool(ctx, call.name, call.arguments)
            traces.append(
                ToolTrace(
                    name=call.name,
                    arguments=call.arguments,
                    ok=result.ok,
                    summary=result.summary,
                )
            )
            citations.extend(result.citations)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.summary}
            )

    gathered = "；".join(t.summary for t in traces if t.ok)
    return AgentAnswer(
        f"（模型来回轮次用尽）先把查到的给你：{gathered or '没有查到信息'}",
        citations=tuple(dict.fromkeys(citations)),
        tools=tuple(traces),
        mode="llm",
        error="max_rounds",
    )
