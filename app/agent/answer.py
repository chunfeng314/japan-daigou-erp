"""助手回答的数据结构（单独放，避免 rules 与 service 循环导入）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolTrace:
    """记录"为了回答这个问题，调了哪个工具、拿到什么" —— 页面上展示给用户核对。"""

    name: str
    arguments: dict
    ok: bool
    summary: str


@dataclass(frozen=True)
class AgentAnswer:
    answer: str
    citations: tuple[str, ...] = ()
    tools: tuple[ToolTrace, ...] = field(default_factory=tuple)
    #: rules = 规则模式（未配模型凭据）; llm = 模型模式
    mode: str = "rules"
    error: str = ""
