"""AI 助手页面。

用 GET（`?q=`）而不是 POST：答案可以被收藏/分享，也不用管 CSRF。
助手目前**只读**，没有任何写操作。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..agent import ask, is_configured
from ..db import get_db
from ..deps import require_user
from ..models import User
from ..templating import templates

router = APIRouter(tags=["assistant"])

SAMPLE_QUESTIONS = (
    "我有哪些待办",
    "有哪些卡单",
    "待上海收货的有几单",
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
            "samples": SAMPLE_QUESTIONS,
            "llm_configured": is_configured(),
        },
    )
