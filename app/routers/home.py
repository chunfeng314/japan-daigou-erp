"""首页：按角色显示各自待办 + 概览。

待办口径全部在 `app/todo_service.py` —— 那里同时被 AI 管家复用，
保证「首页显示的」和「AI 回答的」永远一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_user
from ..models import User
from ..templating import templates
from ..todo_service import count_orders, status_overview, todos_for

router = APIRouter(tags=["home"])


@router.get("/")
def home(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "todos": todos_for(db, user),
            "status_counts": status_overview(db),
            "total_orders": count_orders(db),
        },
    )
