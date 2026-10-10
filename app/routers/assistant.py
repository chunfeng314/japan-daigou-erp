"""AI 助手页面（对话式）。

- `GET /assistant?q=` —— 首屏，也用于收藏/分享某个答案；
- `POST /assistant/ask` —— 聊天用，返回**一段消息片段**，htmx 追加到消息列表末尾。

选 htmx 而不是自己写 fetch 渲染，是为了跟项目"刻意不引入前端构建链"的决定一致，
且手机端天然可用。助手目前**只读**，没有任何写操作。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..agent import ask, is_configured
from ..db import get_db
from ..deps import require_user
from ..models import User
from ..templating import templates

router = APIRouter(tags=["assistant"])

SAMPLE_QUESTIONS = (
    "现在有什么需要我操心的",
    "我该先处理哪一单",
    "有哪些卡单",
    "采购成本一共花了多少钱",
)


@router.get("/assistant")
def assistant(
    request: Request,
    q: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    question = q.strip()
    answer = ask(db, user, question) if question else None
    return templates.TemplateResponse(
        request,
        "assistant.html",
        {
            "user": user,
            "q": q,
            "answer": answer,
            "turn": {"question": question, "answer": answer} if answer else None,
            "samples": SAMPLE_QUESTIONS,
            "llm_configured": is_configured(),
        },
    )


@router.post("/assistant/ask", response_class=HTMLResponse)
def assistant_ask(
    request: Request,
    question: str = Form(""),
    history: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    question = (question or "").strip()
    if not question:
        return HTMLResponse("", status_code=400)

    try:
        parsed_history = json.loads(history) if history.strip() else []
    except ValueError:
        parsed_history = []

    answer = ask(db, user, question, history=parsed_history)
    return templates.TemplateResponse(
        request,
        "_chat_turn.html",
        {
            "user": user,
            "turn": {"question": question, "answer": answer},
            "llm_configured": is_configured(),
        },
    )
