"""应用入口。

本地启动：
    .venv/bin/uvicorn app.main:app --reload --port 8000
生产（东京机）：systemd 托管 uvicorn，前置 Caddy/Nginx 反代 + HTTPS。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .db import init_db
from .deps import Forbidden, LoginRequired, login_redirect_path
from .routers import assistant, auth, briefing, home, orders, stages, uploads, users
from .templating import templates


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # 幂等建表，重启不丢数据
    yield


app = FastAPI(title=config.APP_TITLE, version=__version__, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")

app.include_router(auth.router)
app.include_router(home.router)
app.include_router(assistant.router)
app.include_router(briefing.router)
app.include_router(orders.router)
app.include_router(orders.api_router)
app.include_router(stages.router)
app.include_router(uploads.router)
app.include_router(users.router)


@app.exception_handler(LoginRequired)
async def _handle_login_required(request: Request, _exc: LoginRequired):
    return RedirectResponse(login_redirect_path(request), status_code=303)


@app.exception_handler(Forbidden)
async def _handle_forbidden(request: Request, exc: Forbidden):
    return templates.TemplateResponse(
        request, "forbidden.html", {"message": exc.message}, status_code=403
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> PlainTextResponse:
    return PlainTextResponse(f"ok {__version__}")
