"""本地全流程彩排：不依赖任何服务器，走一遍真实业务链路。

    .venv/bin/python scripts/local_rehearsal.py            # 跑一遍
    .venv/bin/python scripts/local_rehearsal.py --headed   # 显示浏览器窗口（便于围观）
    .venv/bin/python scripts/local_rehearsal.py --keep     # 保留临时数据以便排查

为什么要有这个：**上线前必须先在本地跑通**。服务器上再小的问题，
部署完才发现都要重启一次、排查成本高一个量级。

特点：
- **完全隔离**：用临时目录 + 临时库 + 独立端口，绝不碰你的开发数据；
- **走真浏览器**：关键步骤用 Playwright 点真实页面（含前端 JS），
  不是直接调接口 —— 否则测不到"按钮点不动""JS 报错"这类问题；
- **不中断**：某一步失败也继续往下跑，最后给完整报告（不然只能看到第一个坑）。

⚠️ 环节表单在折叠的 `<details>` 里，脚本会先展开再点真实按钮，
   不是走 JS 绕过 UI —— 就是要验证用户真的能点得动。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

# 只读取配置（拿模型凭据），**不使用它的数据库**
from app import config as app_config  # noqa: E402

CHROMIUM = Path(
    os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/"
        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    )
)
FIXTURE = PROJECT / "tests" / "fixtures" / "order_screenshot.png"
ADMIN_PW = "rehearsal-admin-pw"
USER_PW = "rehearsal-user-pw"


# --------------------------------------------------------------------------- #
@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def ok(self, title: str, detail: str = "") -> None:
        self.passed.append(f"{title}{('：' + detail) if detail else ''}")
        print(f"  ✅ {title}{('  ' + detail) if detail else ''}")

    def fail(self, title: str, detail: str = "") -> None:
        self.failed.append((title, detail))
        print(f"  ❌ {title}  {detail}")

    def step(self, title: str):
        return _Step(self, title)


class _Step:
    def __init__(self, report: Report, title: str) -> None:
        self.report = report
        self.title = title

    def __enter__(self):
        print(f"\n▶ {self.title}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.report.ok(self.title)
        else:
            self.report.fail(self.title, f"{exc_type.__name__}: {exc}")
        return True  # 继续往下跑


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Rehearsal:
    def __init__(self, *, headed: bool = False, keep: bool = False) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="erp-rehearsal-"))
        self.db_path = self.workdir / "rehearsal.sqlite3"
        self.uploads = self.workdir / "uploads"
        self.backups = self.workdir / "backups"
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.headed = headed
        self.keep = keep
        self.server: subprocess.Popen | None = None
        self.report = Report()
        self.llm_ready = bool(
            app_config.LLM_BASE_URL and app_config.LLM_API_KEY and app_config.LLM_MODEL
        )
        self.order_id: int | None = None
        self.order_no: str | None = None
        self._current_user = ""

    # ---------------- 环境 ----------------
    def _env(self) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "ERP_DB_PATH": str(self.db_path),
                "ERP_UPLOAD_DIR": str(self.uploads),
                "ERP_SECRET_KEY": "rehearsal-" + "k" * 40,
                "ERP_COOKIE_SECURE": "0",
                "ERP_DISPLAY_TZ": "Asia/Shanghai",
                "ERP_STORAGE": "local",
                # 管理员账号：init_db 会用它建第一个账号；不给的话它会随机生成密码，
                # 我们就登不进去了（第一版就踩了这个）
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD": ADMIN_PW,
                "ADMIN_DISPLAY_NAME": "彩排管理员",
            }
        )
        if self.llm_ready:
            env.update(
                {
                    "ERP_LLM_BASE_URL": app_config.LLM_BASE_URL,
                    "ERP_LLM_API_KEY": app_config.LLM_API_KEY,
                    "ERP_LLM_MODEL": app_config.LLM_MODEL,
                }
            )
        return env

    def start_server(self) -> None:
        subprocess.run(
            [str(PROJECT / ".venv" / "bin" / "python"), "-m", "scripts.init_db"],
            cwd=str(PROJECT), env=self._env(), check=True, capture_output=True,
        )
        log = (self.workdir / "server.log").open("wb")
        self.server = subprocess.Popen(
            [
                str(PROJECT / ".venv" / "bin" / "uvicorn"), "app.main:app",
                "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning",
            ],
            cwd=str(PROJECT), env=self._env(), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        for _ in range(60):
            time.sleep(0.4)
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                continue
        raise RuntimeError(f"服务没起来，看 {self.workdir / 'server.log'}")

    def stop(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
        if self.keep:
            print(f"\n临时数据保留在：{self.workdir}")
        else:
            shutil.rmtree(self.workdir, ignore_errors=True)

    # ---------------- 浏览器辅助 ----------------
    def login(self, page, username: str, password: str) -> None:
        page.goto(f"{self.base}/login")
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("load")
        if "/login" in page.url:
            raise RuntimeError(f"{username} 登录失败")

    def logout(self, context) -> None:
        context.clear_cookies()

    def advance(self, page, to_status: str, fields: dict) -> None:
        """展开对应环节的折叠面板，填字段，点真实按钮。"""
        info = page.evaluate(
            """(toStatus) => {
              const forms = [...document.querySelectorAll('form')];
              const form = forms.find(
                f => (f.querySelector('[name=to_status]') || {}).value === toStatus);
              if (!form) {
                const available = forms
                  .map(f => (f.querySelector('[name=to_status]') || {}).value)
                  .filter(Boolean);
                const badge = document.querySelector('.page-title .badge');
                return {found: false, available, status: badge ? badge.textContent.trim() : '?'};
              }
              if (form.closest('details')) form.closest('details').open = true;
              form.setAttribute('data-rehearsal', '1');
              return {found: true};
            }""",
            to_status,
        )
        if not info["found"]:
            raise RuntimeError(
                f"页面上没有推进到「{to_status}」的表单"
                f"（当前状态：{info['status']}；当前账号能做的：{info['available'] or '无'}）"
            )
        for name, value in fields.items():
            selector = f'form[data-rehearsal] [name="{name}"]'
            element = page.query_selector(selector)
            if element is None:
                continue
            tag = element.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                element.select_option(str(value))
            elif element.get_attribute("type") == "checkbox":
                if value:
                    element.check()
            else:
                element.fill(str(value))
        page.click('form[data-rehearsal] button[type="submit"]')
        page.wait_for_load_state("load")

    # ---------------- 数据库直连（仅用于模拟时间流逝） ----------------
    def backdate_last_log(self, days: int) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT id FROM status_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE status_logs SET created_at = datetime('now', ?) WHERE id = ?",
                    (f"-{days} days", row[0]),
                )
                connection.commit()
        finally:
            connection.close()

    # ---------------- 汇总 ----------------
    def summary(self) -> int:
        print("\n" + "=" * 66)
        total = len(self.report.passed) + len(self.report.failed)
        if self.report.failed:
            print(f"结果：{len(self.report.passed)}/{total} 项通过，{len(self.report.failed)} 项失败")
            print("\n失败项：")
            for title, detail in self.report.failed:
                print(f"  ❌ {title}  {detail}")
            print("=" * 66)
            return 1
        print(f"结果：全部 {total} 项通过 ✅  本地全流程彩排成功")
        print("=" * 66)
        return 0


# --------------------------------------------------------------------------- #
def run(r: Rehearsal) -> int:
    from playwright.sync_api import sync_playwright

    if not CHROMIUM.exists():
        print(f"找不到浏览器：{CHROMIUM}\n先装：.venv/bin/pip install playwright")
        return 2

    r.start_server()
    print(f"临时环境：{r.workdir}")
    print(f"服务地址：{r.base}")
    print(f"模型凭据：{'已配置（截图识别与助手可用）' if r.llm_ready else '未配置（相关步骤会自动跳过）'}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=str(CHROMIUM), headless=not r.headed)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        # ---------- 1. 管理员建号 ----------
        with r.report.step("管理员登录并创建三个角色账号"):
            r.login(page, "admin", ADMIN_PW)
            for username, display, role in [
                ("li", "李四", "sales"),
                ("wang", "王采购", "purchaser"),
                ("wh", "赵仓管", "warehouse"),
            ]:
                page.goto(f"{r.base}/admin/users")
                page.fill('form[action="/admin/users"] [name="username"]', username)
                page.fill('form[action="/admin/users"] [name="display_name"]', display)
                page.select_option('form[action="/admin/users"] [name="role"]', role)
                page.fill('form[action="/admin/users"] [name="password"]', USER_PW)
                page.click('form[action="/admin/users"] button[type="submit"]')
                page.wait_for_load_state("load")
            page.goto(f"{r.base}/admin/users")
            body = page.content()
            for token in ("李四", "王采购"):
                if token not in body:
                    raise RuntimeError(f"账号 {token} 没出现在账号列表里")

        # ---------- 2. 销售录单（截图自动识别） ----------
        with r.report.step("销售录单：上传订单截图 → 自动预填 → 提交"):
            r.logout(context)
            r.login(page, "li", USER_PW)
            page.goto(f"{r.base}/orders/new")

            if r.llm_ready:
                page.set_input_files("#extract-input", str(FIXTURE))
                page.wait_for_function(
                    "() => { const s = document.getElementById('extract-status');"
                    "return s && s.textContent && !s.textContent.includes('正在识别'); }",
                    timeout=90000,
                )
                status = page.inner_text("#extract-status")
                product = page.input_value('[name="product_desc"]')
                amount = page.input_value('[name="sale_amount"]')
                if "香水" not in product:
                    raise RuntimeError(f"截图没识别出商品名，识别结果={product!r}（提示：{status}）")
                if amount.replace(",", "") not in ("8400", "8400.0"):
                    raise RuntimeError(f"截图没识别出金额：{amount!r}")
                print(f"     截图识别：商品名与金额已自动填好（{status[:40]}…）")
            else:
                page.fill('[name="product_desc"]', "【限定】香水 100ml 未開封 正規品")
                page.fill('[name="quantity"]', "2")
                page.fill('[name="sale_amount"]', "8400")
                print("     （未配模型凭据，跳过识别，手工填表）")

            if not page.input_value('[name="order_date"]'):
                page.fill('[name="order_date"]', "2026-10-10")
            if not page.input_value('[name="quantity"]'):
                page.fill('[name="quantity"]', "2")
            if not page.input_value('[name="sale_amount"]'):
                page.fill('[name="sale_amount"]', "8400")

            # ⚠️ 必须用 #submit-btn，不能写 button[type="submit"] ——
            # 顶栏的「退出」也是 submit 按钮且在 DOM 里排在前面，裸选择器会点它（踩过）
            page.click("#submit-btn")
            page.wait_for_load_state("load")
            if "/orders/" not in page.url or page.url.endswith("/new"):
                raise RuntimeError(f"提交后没跳到详情页：{page.url}")
            # 重定向会带 ?msg=…，取 ID 要先剥掉查询串
            r.order_id = int(page.url.split("/orders/")[-1].split("?")[0])
            r.order_no = page.inner_text(".page-title .mono, .page-title").split()[0]
            body = page.content()
            if "待采购" not in body:
                raise RuntimeError("新单状态不是「待采购」")
            if "订单截图" not in body:
                raise RuntimeError("截图没挂到订单上")

        # ---------- 3~7. 全链路推进 ----------
        # 字段必须对上 state_machine 里的 required_fields，否则服务端会拒绝（服务端是对的）
        chain = [
            ("wang", "purchaser", "采购中", {}, "采购登记：开始采购"),
            ("wang", "purchaser", "待上海收货", {
                "purchase_price": "128", "purchase_currency": "CNY",
                "source_platform": "淘宝", "domestic_tracking_no": "SF1234567890",
                "carrier": "顺丰",
            }, "采购完成：登记成本 + 国内快递单号"),
            ("wh", "warehouse", "已收货待安排带货",
             {"received_date": "2026-10-11", "handler": "赵仓管"}, "上海仓：确认收货"),
            ("wh", "warehouse", "已安排带货", {
                "carrier_person": "带货人张三", "flight_date": "2026-10-15", "flight_no": "NH920",
            }, "上海仓：安排带货"),
            ("wh", "warehouse", "已发货",
             {"ship_date": "2026-10-16", "tracking_no": "JP1234567890"}, "上海仓：登记发货给买家"),
            ("li", "sales", "待结算", {}, "已发货 → 待结算"),
            ("li", "sales", "已完成", {
                "settle_date": "2026-10-20", "final_amount": "8200", "currency": "JPY",
            }, "销售：录入结算 → 订单完成"),
        ]
        for username, role, to_status, fields, title in chain:
            with r.report.step(f"{title}（{username} / {role}）"):
                if username != r._current_user:
                    r.logout(context)
                    r.login(page, username, USER_PW)
                    r._current_user = username
                page.goto(f"{r.base}/orders/{r.order_id}")
                r.advance(page, to_status, fields)
                # 断言要**比对状态徽章**，不能只判断页面上出现过这几个字 ——
                # 「待上海收货」在提示文案里也会出现，弱断言会把失败静默吞掉（踩过）
                badge = page.inner_text(".page-title .badge").strip()
                if badge != to_status:
                    raise RuntimeError(
                        f"推进到「{to_status}」失败：状态徽章现在是「{badge}」"
                        f"（页面提示：{' '.join(page.inner_text('.alert').split())[:120]}）"
                    )

        r._current_user = None

        # ---------- 8. 异常追踪 ----------
        with r.report.step("登记少货异常 → 非管理员不能了结 → 管理员了结"):
            r.logout(context)
            r.login(page, "li", USER_PW)
            page.goto(f"{r.base}/orders/{r.order_id}")
            # 异常登记表单在折叠面板里，先展开（和 advance 同样的道理）
            page.evaluate(
                """() => {
                  const form = [...document.querySelectorAll('form')]
                    .find(f => f.querySelector('[name=exception_type]'));
                  if (form && form.closest('details')) form.closest('details').open = true;
                }"""
            )
            page.select_option('[name="exception_type"]', index=1)
            page.fill('[name="qty_affected"]', "1")
            page.fill('[name="detail"]', "买家反馈少了一件")
            page.click('form[action$="/exceptions"] button[type="submit"]')
            page.wait_for_load_state("load")
            if "异常" not in page.content():
                raise RuntimeError("异常没登记成功")

            r.logout(context)
            r.login(page, "li", USER_PW)
            page.goto(f"{r.base}/orders/{r.order_id}")
            if "仅管理员可操作" not in page.content():
                raise RuntimeError("非管理员不该看到「了结」按钮")

            r.logout(context)
            r.login(page, "admin", ADMIN_PW)
            page.goto(f"{r.base}/orders/{r.order_id}")
            page.fill('[name="resolution"]', "已补发一件")
            page.click('form[action$="/resolve"] button[type="submit"]')
            page.wait_for_load_state("load")
            if "已解决" not in page.content():
                raise RuntimeError("管理员了结失败")

        # ---------- 9. 卡单检测 ----------
        with r.report.step("第二单卡在中间：列表标「超时」+ 日报点名它"):
            # ⚠️ 必须**另建一单**卡在中间 —— 上一单已经「已完成」，终态不判超时。
            #    真实场景也是这样：一单在往前走，另一单卡住了没人管。
            r.logout(context)
            r.login(page, "li", USER_PW)
            page.goto(f"{r.base}/orders/new")
            page.fill('[name="product_desc"]', "卡住的单：限定 徽章")
            page.fill('[name="order_date"]', "2026-10-01")
            page.fill('[name="quantity"]', "1")
            page.fill('[name="sale_amount"]', "1200")
            page.click("#submit-btn")
            page.wait_for_load_state("load")
            stuck_no = page.inner_text(".page-title .mono, .page-title").split()[0]
            r.backdate_last_log(days=9)   # 模拟已经躺了 9 天没人管

            r.logout(context)
            r.login(page, "admin", ADMIN_PW)
            page.goto(f"{r.base}/orders")
            body = page.content()
            if "超时" not in body:
                raise RuntimeError("列表页没有出现「超时」标记")
            if stuck_no not in body:
                raise RuntimeError(f"卡住的那单 {stuck_no} 没出现在列表里")

            page.goto(f"{r.base}/briefing")
            briefing = page.content()
            if "卡住" not in briefing:
                raise RuntimeError("日报里没有「卡住」结论")
            if stuck_no not in briefing:
                raise RuntimeError("日报没有点名那单卡在哪")
            if "复制日报" not in briefing:
                raise RuntimeError("日报缺少复制按钮")

        # ---------- 10. 可见性 ----------
        with r.report.step("权限隔离：销售乙看不到销售甲的单"):
            connection = sqlite3.connect(r.db_path)
            try:
                connection.execute(
                    "INSERT INTO users (username, display_name, role, password_hash, "
                    "is_active, created_at) VALUES (?,?,?,?,1,datetime('now'))",
                    ("zhao", "销售乙", "sales", _hash(USER_PW)),
                )
                connection.commit()
            finally:
                connection.close()

            r.logout(context)
            r.login(page, "zhao", USER_PW)
            # ⚠️ 断言要看**表格内容**，不能看整页 —— 搜订单号时那个号会回显在搜索框里，
            #    整页匹配会误判成"看到了别人的单"（第一版就踩了）
            page.goto(f"{r.base}/orders?q={r.order_no}")
            if "还没有符合条件的订单" not in page.content():
                raise RuntimeError("销售乙竟然在列表里看到了别人的订单")
            page.goto(f"{r.base}/orders/{r.order_id}")
            if "没有找到" not in page.content() and "无权" not in page.content():
                body = page.content()
                if r.order_no in body.split("<table")[0]:
                    raise RuntimeError("销售乙竟然打开了别人的订单详情")
            page.goto(f"{r.base}/briefing")
            if "没有卡单，也没有未结束的订单" not in page.content():
                raise RuntimeError("销售乙的日报里不该有别人的单")

        # ---------- 11. 导出 ----------
        with r.report.step("管理员导出 CSV（含停留/超时/异常列）"):
            r.logout(context)
            r.login(page, "admin", ADMIN_PW)
            response = context.request.get(f"{r.base}/orders/export.csv")
            text = response.text().lstrip("﻿")
            if r.order_no not in text:
                raise RuntimeError("导出里没有这单")
            for header in ("当前状态停留天数", "是否超时", "有无未了结异常"):
                if header not in text.splitlines()[0]:
                    raise RuntimeError(f"导出缺列：{header}")

        # ---------- 12. AI 助手 ----------
        if r.llm_ready:
            with r.report.step("AI 助手：能回答，且拿不到别人的单"):
                page.goto(f"{r.base}/assistant?q={r.order_no} 现在什么状态")
                body = page.content()
                if "模型模式" not in body:
                    raise RuntimeError("没进模型模式")
                if r.order_no not in body:
                    raise RuntimeError("助手没引用订单号")

                r.logout(context)
                r.login(page, "zhao", USER_PW)
                page.goto(f"{r.base}/assistant?q={r.order_no} 卖的是什么、多少钱")
                body = page.content()
                if "没查到你名下的" not in body:
                    raise RuntimeError("助手对越权查询没有正确拒绝")
        else:
            print("  ⏭  跳过 AI 助手（未配模型凭据）")

        # ---------- 13. 手机端 ----------
        with r.report.step("手机视口（390×844）巡检：无横向溢出、无小触摸目标"):
            mobile = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2, is_mobile=True, has_touch=True,
            )
            mpage = mobile.new_page()
            mpage.goto(f"{r.base}/login")
            mpage.fill('input[name="username"]', "admin")
            mpage.fill('input[name="password"]', ADMIN_PW)
            mpage.click('button[type="submit"]')
            mpage.wait_for_load_state("load")

            problems = []
            for path in ("/", "/orders", f"/orders/{r.order_id}", "/orders/new",
                         "/briefing", "/assistant", "/admin/users"):
                mpage.goto(f"{r.base}{path}")
                mpage.wait_for_timeout(200)
                found = mpage.evaluate(MOBILE_PROBE)
                if found["overflow"] or found["small"]:
                    problems.append(f"{path}: {found}")
            mobile.close()
            if problems:
                raise RuntimeError("；".join(problems))

        # ---------- 14. 备份与恢复 ----------
        with r.report.step("备份 → 恢复演练：还原后数据与线上完全一致"):
            result = subprocess.run(
                [str(PROJECT / ".venv" / "bin" / "python"), "-m", "scripts.backup_db",
                 "--out", str(r.backups), "--keep-days", "30"],
                cwd=str(PROJECT), env=r._env(), capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"备份失败：{result.stderr.strip()[:200]}")

            produced = sorted(r.backups.glob("erp-*.sqlite3"))
            if not produced:
                raise RuntimeError("没有产出备份文件")
            restored = r.workdir / "restored.sqlite3"
            shutil.copy(produced[-1], restored)
            live = _snapshot(r.db_path)
            back = _snapshot(restored)
            if live != back:
                raise RuntimeError(f"恢复后数据不一致：{live} vs {back}")
            print(f"     还原校验：{' / '.join(f'{k}={v}' for k, v in back.items())}")

        context.close()
        browser.close()

    return r.summary()


MOBILE_PROBE = """() => {
  const de = document.documentElement, vw = window.innerWidth;
  const bad = [];
  document.querySelectorAll('body *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width > vw + 1 && r.height > 0 && getComputedStyle(el).overflowX !== 'auto')
      bad.push(el.tagName.toLowerCase());
  });
  const small = [];
  document.querySelectorAll('button, .btn, a.chip').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.height > 0 && r.height < 40) small.push(el.textContent.trim().slice(0, 8));
  });
  return {overflow: de.scrollWidth > vw + 1, small: small};
}"""


def _hash(password: str) -> str:
    from app.security import hash_password

    return hash_password(password)


def _snapshot(path: Path) -> dict:
    """关键表的行数 —— 恢复演练就是拿它逐项比对。"""
    tables = ("users", "orders", "status_logs", "order_images", "purchases")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        existing = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
            if table in existing
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="日代 ERP 本地全流程彩排")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--keep", action="store_true", help="保留临时数据以便排查")
    args = parser.parse_args()

    rehearsal = Rehearsal(headed=args.headed, keep=args.keep)
    try:
        return run(rehearsal)
    finally:
        rehearsal.stop()


if __name__ == "__main__":
    raise SystemExit(main())
