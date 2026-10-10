"""每日简报页面。规则生成，只读，可复制转发。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..briefing import build
from ..db import get_db
from ..deps import require_user
from ..models import User
from ..templating import templates

router = APIRouter(tags=["briefing"])


@router.get("/briefing")
def briefing_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "briefing.html",
        {"user": user, "briefing": build(db, user)},
    )
