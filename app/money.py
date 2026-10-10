"""金额表达与汇总。

**两条硬约束**（来自开发文档 §2.4，改这个模块时别越界）：
- **分币种各记各的** —— 不做跨币种相加
- **不自动换汇、不算利润** —— 系统里没有汇率配置

所以这里做的只有两件事：**把同一币种的数字加在一起**，以及**把数字写得看得懂**
（`¥8,400 日元` 而不是 `8400.00 JPY`）。

"这单赚了多少"需要汇率口径，属于要老板拍板的事，**不在这里做**。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

#: 币种 → (符号, 行内简写, 分组标题用全名, 小数位)
#:    行内写「￥128.00 元」够短；分组标题要写全「人民币」，不然"支出（元）"看不懂。
#:    JPY 习惯不带小数，人民币带两位。
CURRENCY_META: dict[str, tuple[str, str, str, int]] = {
    "JPY": ("¥", "日元", "日元", 0),
    "CNY": ("￥", "元", "人民币", 2),
}


def format_money(amount: float | None, currency: str | None, *, with_name: bool = True) -> str:
    """金额 → 看得懂的字符串。`None` 返回「—」。"""
    if amount is None:
        return "—"
    symbol, short, full, digits = CURRENCY_META.get(currency or "", ("", currency or "", currency or "", 2))
    text = f"{float(amount):,.{digits}f}"
    head = f"{symbol}{text}" if symbol else text
    if with_name and short:
        return f"{head} {short}"
    return f"{head} {currency}" if currency else head


def currency_label(currency: str | None) -> str:
    """币种全名，给「支出（…）」这类分组标题用。"""
    _, _, full, _ = CURRENCY_META.get(currency or "", ("", currency or "", currency or "", 2))
    return full or "—"


@dataclass(frozen=True)
class MoneyItem:
    label: str
    amount: float
    currency: str
    note: str = ""


@dataclass(frozen=True)
class OrderMoney:
    #: 售价（销售录单时定的预期收入）
    sale: MoneyItem | None
    #: 已登记的支出（采购、运费、平台手续费、退款…），全部混在一个列表里
    expenses: tuple[MoneyItem, ...]
    #: 平台最终结算实收
    settle: MoneyItem | None

    @property
    def totals_by_currency(self) -> tuple[tuple[str, float], ...]:
        """按币种小计 —— **绝不跨币种相加**。"""
        buckets: dict[str, float] = {}
        for item in self.expenses:
            buckets[item.currency] = buckets.get(item.currency, 0.0) + float(item.amount)
        return tuple(sorted(buckets.items()))

    @property
    def has_any(self) -> bool:
        return bool(self.expenses or self.settle)


def collect(db: Session, order) -> OrderMoney:
    """把一张单上散落各处的金额收拢到一处。"""
    from sqlalchemy import select

    from .models import Purchase, Settlement

    expenses: list[MoneyItem] = []

    def _add(label: str, value, currency: str, note: str = "") -> None:
        """0 元不记 —— 卡片里一排 0 是噪音。"""
        if value is None or float(value) == 0:
            return
        expenses.append(MoneyItem(label, float(value), currency, note))

    purchase = db.scalar(select(Purchase).where(Purchase.order_id == order.id))
    if purchase is not None and purchase.purchase_price is not None:
        currency = purchase.purchase_currency or "CNY"
        # 采购环节的三个金额共用 purchase_currency —— 它们都是在国内花的，
        # 所以**运费/其他费用不需要单独的币种字段**，继承即可（这也是原来显示成裸数字的原因）
        _add("采购成本", purchase.purchase_price, currency, purchase.source_platform or "")
        _add("国内运费", purchase.domestic_shipping_fee, currency, purchase.carrier or "")
        _add("其他费用", purchase.other_fee, currency)

    settle: MoneyItem | None = None
    settlement = db.scalar(select(Settlement).where(Settlement.order_id == order.id))
    if settlement is not None and settlement.final_amount is not None:
        currency = settlement.currency or order.sale_currency or "JPY"
        settle = MoneyItem("平台实收", float(settlement.final_amount), currency)
        _add("平台手续费", settlement.platform_fee, currency)
        _add("日本国内运费", settlement.japan_shipping, currency)
        _add("退款", settlement.refund, currency)
        _add("其他扣款", settlement.other_deduction, currency)

    sale = MoneyItem(
        "售价", float(order.sale_amount or 0), order.sale_currency or "JPY"
    )
    return OrderMoney(sale=sale, expenses=tuple(expenses), settle=settle)
