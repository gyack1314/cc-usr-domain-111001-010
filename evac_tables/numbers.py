"""数量解析：中文数字、人/百人单位与不精确数量识别。

只支持题目要求的 一 到 九、十、百 及其组合。任何约数、多人、区间或
无法确认的数字都返回 ``None``，由调用方输出隔离记录，绝不截断成精确值。
"""
from __future__ import annotations

import re

from .dom import normalize_text

_CN_DIGITS = {
    "零": 0, "〇": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100}
_CN_CHARS = set(_CN_DIGITS) | set(_CN_UNITS)
_NUM_TOKEN = r"(?:[0-9]+|[一二三四五六七八九十百两零〇]+)"
_RANGE_SEP = r"(?:-|~|～|—|–|至|到)"


def parse_chinese_numeral(text):
    """解析一~九、十、百的合法组合；非法或歧义组合返回 ``None``。"""
    text = (text or "").strip()
    if not text or any(ch not in _CN_CHARS for ch in text):
        return None
    total = 0
    current = 0
    last_unit = 1000
    saw_any = False
    zero_since_unit = False
    for ch in text:
        if ch in ("零", "〇"):
            if current != 0:
                return None
            zero_since_unit = True
            continue
        if ch in _CN_DIGITS:
            if current != 0:
                # 连续数字（如“二三”）不是合法组合。
                return None
            current = _CN_DIGITS[ch]
            saw_any = True
            continue
        unit = _CN_UNITS[ch]
        if unit >= last_unit:
            # 单位重复或由小单位回到大单位，均不合法。
            return None
        if current == 0:
            current = 1
        total += current * unit
        current = 0
        last_unit = unit
        zero_since_unit = False
        saw_any = True
    if not saw_any:
        return None
    # “一百一”之类既可能是一百零一也可能是一百一十，属于歧义写法。
    if last_unit == 100 and current != 0 and not zero_since_unit:
        return None
    return total + current


def parse_exact_amount(token):
    token = (token or "").strip()
    if not token:
        return None
    if re.fullmatch(r"[0-9]+", token):
        return int(token)
    if re.fullmatch(_NUM_TOKEN, token):
        return parse_chinese_numeral(token)
    return None


def header_unit(labels):
    """从表头标签推导人数单位。

    返回 ``('person'|'hundred'|None|'ambiguous', evidence)``。显式写出的
    单元格单位在调用方优先于这里的结果。
    """
    units = []
    evidence = []
    for label in labels:
        text = normalize_text(label.text if hasattr(label, "text") else str(label))
        if "百人" in text:
            units.append("hundred")
            evidence.append(text)
        elif "人" in text:
            units.append("person")
            evidence.append(text)
    unique = {u for u in units}
    if len(unique) > 1:
        return "ambiguous", evidence
    if unique:
        return next(iter(unique)), evidence
    return None, evidence


_INEXACT_MARKERS = (
    "约", "大约", "大概", "近", "将近", "余", "左右", "上下",
    "多", "若干", "区间", "不等", "逾", "超过", "以内", "以上",
    "以下", "至少", "至多",
)


def detect_inexact(text):
    """返回不精确原因列表；空列表表示未发现约数/区间。"""
    text = normalize_text(text)
    reasons = []
    for marker in _INEXACT_MARKERS:
        if marker in text:
            reasons.append(f"含“{marker}”")
    if re.search(_NUM_TOKEN + r"\s*" + _RANGE_SEP + r"\s*" + _NUM_TOKEN, text):
        reasons.append("区间数量")
    if len(re.findall(_NUM_TOKEN, text)) > 1 and "区间数量" not in reasons:
        reasons.append("出现多个数字")
    if re.search(r"[0-9]+\s*\.\s*[0-9]+", text):
        reasons.append("非整数")
    return reasons


class Quantity:
    __slots__ = ("value", "status", "reason", "unit_source", "detail")

    def __init__(self, value=None, status="exact", reason="", unit_source="", detail=None):
        self.value = value
        self.status = status
        self.reason = reason
        self.unit_source = unit_source
        self.detail = detail or {}

    @property
    def ok(self):
        return self.status == "exact" and self.value is not None

    def as_dict(self):
        data = {
            "status": self.status,
            "unit_source": self.unit_source,
        }
        if self.value is not None:
            data["participants_people"] = self.value
        if self.reason:
            data["reason"] = self.reason
        if self.detail:
            data["detail"] = self.detail
        return data


def parse_quantity(cell_text, header_unit_value):
    """把单元格文字解析成整数人数。

    ``header_unit_value`` 为 ``person`` / ``hundred`` / ``None`` /
    ``ambiguous``。单元格里显式写的 ``人`` 或 ``百人`` 优先。
    """
    raw = normalize_text(cell_text)
    if not raw:
        return Quantity(status="missing", reason="数量为空")

    inexact = detect_inexact(raw)
    if inexact:
        return Quantity(status="inexact", reason="；".join(inexact), detail={"raw": raw})

    if header_unit_value == "ambiguous":
        return Quantity(status="ambiguous_unit", reason="表头人数单位同时出现人与百人",
                        detail={"raw": raw})

    explicit = None
    match = re.search(r"(百人|人)\s*$", raw)
    if match:
        explicit = match.group(1)
        amount_text = raw[:match.start()].strip(" \t：:，,、")
    else:
        amount_text = raw.strip(" \t：:，,、")

    amount = parse_exact_amount(amount_text)
    if amount is None:
        return Quantity(status="unparseable", reason="数量文字无法解析",
                        detail={"raw": raw, "amount_text": amount_text})

    if explicit == "百人":
        factor, source = 100, "cell"
    elif explicit == "人":
        factor, source = 1, "cell"
    elif header_unit_value == "hundred":
        factor, source = 100, "header"
    elif header_unit_value == "person":
        factor, source = 1, "header"
    else:
        factor, source = 1, "default"

    return Quantity(value=amount * factor, unit_source=source, detail={
        "raw": raw,
        "amount": amount,
        "factor": factor,
        "explicit_cell_unit": explicit,
        "header_unit": header_unit_value,
    })
