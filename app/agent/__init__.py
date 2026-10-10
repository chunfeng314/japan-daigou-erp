"""AI 管家（二期）。

对外只需要这三个入口：

    from app.agent import ask, is_configured, set_client

- `ask(db, user, question)` → AgentAnswer（含答案、依据、调过的工具）
- `is_configured()` → 是否配了模型凭据（没配就走规则模式）
- `set_client(...)` → 仅供测试注入模型替身

⚠️ 安全边界见 `tools.py`：权限过滤在**工具层**执行，不靠提示词。
"""

from .answer import AgentAnswer, ToolTrace  # noqa: F401
from .llm import LLMError, ScriptedLLM, get_llm, is_configured, reset_client, set_client  # noqa: F401
from .service import ask  # noqa: F401
from .tools import TOOLS, AgentContext, ToolResult, ToolSpec, describe_tools, run_tool  # noqa: F401

__all__ = [
    "AgentAnswer",
    "ToolTrace",
    "AgentContext",
    "ToolResult",
    "ToolSpec",
    "TOOLS",
    "ask",
    "describe_tools",
    "run_tool",
    "is_configured",
    "get_llm",
    "set_client",
    "reset_client",
    "ScriptedLLM",
    "LLMError",
]
