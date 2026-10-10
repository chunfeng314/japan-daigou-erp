"""上线前自检：把「配置错了但跑起来看着正常」这类问题提前揪出来。

    .venv/bin/python -m scripts.preflight

退出码 0 = 没有 FAIL（有 WARN 也算过）；1 = 有 FAIL，别上线。

设计立场：**宁可吵，也不放过**。这些问题一旦上了线，
表现往往是「偶发 401」「cookie 不生效」「备份恢复后少数据」——极难排查。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

INSECURE_SECRETS = {"", "dev-insecure-secret-change-me", "changeme", "secret"}
MIN_SECRET_LEN = 32
MIN_PYTHON = (3, 11)
MIN_FREE_MB = 500


@dataclass(frozen=True)
class Check:
    level: str  # ok / warn / fail
    title: str
    detail: str = ""


def check_secret_key() -> Check:
    key = config.SECRET_KEY or ""
    if key in INSECURE_SECRETS:
        return Check("fail", "会话密钥是默认值", "必须换成一串随机值，否则会话可被伪造")
    if len(key) < MIN_SECRET_LEN:
        return Check("fail", "会话密钥太短", f"当前 {len(key)} 字符，建议 >= {MIN_SECRET_LEN}")
    return Check("ok", "会话密钥已设置", f"{len(key)} 字符")


def check_cookie_secure() -> Check:
    if config.COOKIE_SECURE:
        return Check("ok", "会话 cookie 带 Secure")
    return Check(
        "warn",
        "会话 cookie 未带 Secure",
        "如果站点已是 HTTPS，请把 ERP_COOKIE_SECURE=1 —— 否则 cookie 可能在明文信道被截获",
    )


def check_database() -> list[Check]:
    checks: list[Check] = []
    db_path = config.DB_PATH

    if not db_path.exists():
        checks.append(
            Check("warn", "数据库还不存在", f"{db_path} —— 首次启动会自动建表，别忘了跑 init_db")
        )
        return checks

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            orders = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        return [Check("fail", "数据库打不开或已损坏", str(exc))]

    if integrity[0] != "ok":
        checks.append(Check("fail", "数据库完整性检查未通过", str(integrity[0])))
    else:
        checks.append(Check("ok", "数据库完整性检查通过"))

    free_mb = shutil.disk_usage(db_path.parent).free / 1024 / 1024
    if free_mb < MIN_FREE_MB:
        checks.append(Check("fail", "磁盘剩余空间不足", f"{free_mb:.0f} MB < {MIN_FREE_MB} MB"))
    else:
        checks.append(Check("ok", "磁盘剩余空间充足", f"{free_mb / 1024:.1f} GB"))

    if users == 0:
        checks.append(Check("fail", "一个账号都没有", "跑一下 python -m scripts.init_db"))
    else:
        checks.append(Check("ok", "账号已创建", f"{users} 个"))
    checks.append(Check("ok", "订单数据", f"{orders} 单"))
    return checks


def check_storage() -> Check:
    if config.STORAGE_BACKEND == "cos":
        missing = [
            name
            for name, value in {
                "COS_SECRET_ID": config.COS_SECRET_ID,
                "COS_SECRET_KEY": config.COS_SECRET_KEY,
                "COS_REGION": config.COS_REGION,
                "COS_BUCKET": config.COS_BUCKET,
            }.items()
            if not value
        ]
        if missing:
            return Check("fail", "选了 COS 存储但配置不全", "缺：" + "、".join(missing))
        try:
            import qcloud_cos  # noqa: F401
        except ImportError:
            return Check(
                "fail", "选了 COS 存储但没装 SDK", ".venv/bin/pip install cos-python-sdk-v5"
            )
        return Check(
            "warn",
            "COS 存储已配置",
            "记得在 COS 控制台配 CORS（允许来源=本站域名，方法=PUT/GET/HEAD），否则浏览器直传会被拦",
        )

    uploads = config.UPLOAD_DIR
    try:
        uploads.mkdir(parents=True, exist_ok=True)
        probe = uploads / ".preflight"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("fail", "本地图片目录不可写", f"{uploads}：{exc}")
    return Check(
        "warn", "使用的是本地存储", f"{uploads} —— 多机部署前请切 COS，否则各机器图片不互通"
    )


def check_llm() -> Check:
    parts = {
        "ERP_LLM_BASE_URL": config.LLM_BASE_URL,
        "ERP_LLM_API_KEY": config.LLM_API_KEY,
        "ERP_LLM_MODEL": config.LLM_MODEL,
    }
    filled = [name for name, value in parts.items() if value]
    if not filled:
        return Check("warn", "未配置模型凭据", "助手会走规则模式；截图识别不可用（其余功能正常）")
    if len(filled) != len(parts):
        missing = [name for name, value in parts.items() if not value]
        return Check("fail", "模型凭据只配了一半", "缺：" + "、".join(missing))
    return Check("ok", "模型已配置", config.LLM_MODEL)


def check_python() -> Check:
    if sys.version_info[:2] < MIN_PYTHON:
        return Check("fail", "Python 版本过低", f"{sys.version.split()[0]}，需要 >= 3.11")
    return Check("ok", "Python 版本", sys.version.split()[0])


def check_timezone() -> Check:
    from app.timeutil import display_tz

    tz = display_tz()
    name = str(tz)
    if name in {"UTC", "Etc/UTC"} and config.DISPLAY_TZ not in {"UTC", "Etc/UTC"}:
        return Check("fail", "时区配置无效，已回退 UTC", f"ERP_DISPLAY_TZ={config.DISPLAY_TZ} 不认识")
    return Check("ok", "展示时区", f"{config.DISPLAY_TZ}（界面上时间按它显示）")


def run_all() -> list[Check]:
    checks = [check_python(), check_secret_key(), check_cookie_secure(), check_timezone()]
    checks += check_database()
    checks += [check_storage(), check_llm()]
    return checks


SYMBOLS = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
ORDER = {"fail": 0, "warn": 1, "ok": 2}


def main() -> int:
    print(f"上线自检 —— 项目 {config.BASE_DIR}")
    print(f"数据库  {config.DB_PATH}")
    print("=" * 62)

    checks = run_all()
    for check in sorted(checks, key=lambda c: ORDER[c.level]):
        print(f" {SYMBOLS[check.level]} {check.title}")
        if check.detail:
            print(f"      {check.detail}")

    failures = [c for c in checks if c.level == "fail"]
    warnings = [c for c in checks if c.level == "warn"]
    print("=" * 62)
    print(f"结论：{len(failures)} 项不通过，{len(warnings)} 项提醒。")
    if failures:
        print("👉 不通过的项必须解决后才能上线。")
        return 1
    print("👉 没有阻塞项。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
