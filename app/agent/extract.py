"""订单截图 → 表单字段（多模态提取）。

开发文档 §9 原文：「甩截图 → 多模态提取 → **预填表单** → **人确认**」。

两条设计原则：
1. **模型输出一律不可信**。解析层必须防御：剥 markdown 围栏、只认已知字段、
   逐字段类型校验、日期格式校验。模型吐垃圾进来，最坏结果是"没预填"，不能是"填错"。
2. **只做预填，绝不落库**。本模块不碰数据库写操作 —— 建单仍然走录单表单那条路，
   照样过 `_validate_order_form`。所以模型再怎么胡说，也造不出一张错的订单。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from .llm import LLMClient, LLMError, get_llm

#: 允许被识别的字段（白名单：模型多给的字段一律丢弃）
TEXT_FIELDS = ("platform", "shop", "order_date", "order_time", "product_desc", "sale_currency")
NUMERIC_FIELDS = ("quantity", "sale_amount")
ALL_FIELDS = TEXT_FIELDS + NUMERIC_FIELDS

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

EXTRACT_PROMPT = """你是录单助手。请从这张订单截图里抽取信息，**只输出一个 JSON 对象**，不要任何解释或前后缀。

字段：
- platform        平台名（如 Mercari / メルカリ / 雅虎拍卖）
- shop            店铺名
- order_date      下单日期，格式必须是 YYYY-MM-DD
- order_time      下单时间，格式必须是 HH:MM
- product_desc    商品名称或需求描述（保留截图里的原文语言，中日文都照抄，不要翻译）
- quantity        数量（整数）
- sale_amount     销售金额（纯数字，不要带货币符号或千分位）
- sale_currency   币种，只填 JPY 或 CNY

硬性要求：
1. **看不清、或截图里根本没有的字段，一律填 null。绝对不要猜、不要用常识补全。**
2. 不要换算汇率、不要加减金额、不要自己算总价。
3. 不要翻译商品名。
4. 只输出 JSON，形如 {"platform": null, "shop": null, "order_date": null, "order_time": null,
   "product_desc": null, "quantity": null, "sale_amount": null, "sale_currency": null}
"""


@dataclass
class ExtractResult:
    ok: bool
    fields: dict = field(default_factory=dict)
    #: 识别到的字段名（用于界面提示"AI 填了这几个"）
    filled: tuple[str, ...] = ()
    #: 模型原始输出（排障用，页面上不直接展示给普通用户）
    raw: str = ""
    error: str = ""


def image_content(text: str, image_bytes: bytes, mime: str = "image/png") -> list[dict]:
    """拼成 OpenAI 视觉格式的 content 数组。"""
    payload = base64.b64encode(image_bytes).decode()
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/png'};base64,{payload}"}},
    ]


# --------------------------------------------------------------------------- #
# 防御性解析
# --------------------------------------------------------------------------- #
def _strip_fence(text: str) -> str:
    """模型爱把 JSON 包在 ```json ... ``` 里。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw[3:]
        if raw[:4].lower() == "json":
            raw = raw[4:]
        end = raw.rfind("```")
        if end != -1:
            raw = raw[:end]
    return raw.strip()


def _find_json_object(text: str) -> str | None:
    """从一段话里抠出最外层的 JSON 对象（模型偶尔会加解释性前后缀）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "-", "未知", "看不清"}:
        return None
    return text


def _clean_int(value: object) -> int | None:
    text = _clean_text(value)
    if text is None:
        return None
    match = re.search(r"-?\d+", text.replace(",", ""))
    if not match:
        return None
    number = int(match.group())
    return number if number > 0 else None


def _clean_float(value: object) -> float | None:
    text = _clean_text(value)
    if text is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    number = float(match.group())
    return number if number > 0 else None


def _clean_date(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    # 容忍 2026/10/10、2026年10月10日
    normalised = re.sub(r"[年月]", "-", text).replace("/", "-").replace("日", "").strip("-")
    if DATE_RE.match(normalised):
        try:
            datetime.strptime(normalised, "%Y-%m-%d")
            return normalised
        except ValueError:
            return None
    return None


def _clean_time(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


#: 明确不是我们要的币种。必须先排掉 —— 否则「美元」「港元」「韩元」里的「元」
#: 会被下面的 CNY 规则误判（这个坑是测试逼出来的）。
_OTHER_CURRENCY_HINTS = (
    "美元", "USD", "欧元", "EUR", "港币", "港元", "HKD", "英镑", "GBP",
    "韩元", "KRW", "台币", "新台币", "TWD", "澳元", "AUD", "加元", "CAD",
)


def _clean_currency(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    upper = text.upper()

    if any(hint in upper or hint in text for hint in _OTHER_CURRENCY_HINTS):
        return None

    # 先判日元：中文「日元/日圆」里含「元」，必须先于人民币的「元」判断
    if "JPY" in upper or upper == "JP" or "円" in text or "日元" in text or "日圆" in text:
        return "JPY"
    if (
        "CNY" in upper
        or "RMB" in upper
        or upper == "CN"
        or "元" in text
        or "人民币" in text
        or "块" in text
    ):
        return "CNY"
    # 单独的 ¥ 中日通用，分不出来 → 不猜
    return None


_CLEANERS = {
    "platform": _clean_text,
    "shop": _clean_text,
    "product_desc": _clean_text,
    "sale_currency": _clean_currency,
    "order_date": _clean_date,
    "order_time": _clean_time,
    "quantity": _clean_int,
    "sale_amount": _clean_float,
}


def parse_extraction(text: str) -> dict:
    """把模型的输出解析成干净的字段字典。任何异常都退化成「识别不到」，绝不抛给上层。"""
    body = _find_json_object(_strip_fence(text))
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}

    fields: dict = {}
    for name in ALL_FIELDS:  # 白名单：模型多给的键直接丢
        if name not in data:
            continue
        cleaned = _CLEANERS[name](data[name])
        if cleaned is not None:
            fields[name] = cleaned
    return fields


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def extract_order_fields(
    image_bytes: bytes,
    mime: str = "image/png",
    *,
    client: LLMClient | None = None,
) -> ExtractResult:
    if not image_bytes:
        return ExtractResult(False, error="图片是空的")

    active = client if client is not None else get_llm()
    if active is None:
        return ExtractResult(False, error="未配置模型凭据，截图识别不可用（可以手工录入）")

    try:
        reply = active.chat(
            [{"role": "user", "content": image_content(EXTRACT_PROMPT, image_bytes, mime)}]
        )
    except LLMError as exc:
        return ExtractResult(False, error=f"模型服务出问题了：{exc}")

    raw = (reply.content or "").strip()
    fields = parse_extraction(raw)
    if not fields:
        return ExtractResult(
            True, fields={}, filled=(), raw=raw, error="这张截图里没识别出可用字段，请手工填写"
        )
    return ExtractResult(True, fields=fields, filled=tuple(fields), raw=raw)
