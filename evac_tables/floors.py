"""楼层解析与展开。

支持单层、地下层以及层段：``1F``、``B2``、``1-3F``、``B2-B1``、
``B1-3F``。层段展开后每个楼层都映射回同一条原始行证据，不拆分人数。
跨度上限由 ``extraction-options.json`` 的 ``maximum_floor_span`` 指定；
超过上限时整体隔离，绝不截取前几层。
"""
from __future__ import annotations

import re

from .dom import normalize_text

_ENDPOINT_RE = re.compile(
    r"^(?P<basement>[Bb])?(?P<number>\d+)(?P<suffix>[Ff]|层)?$"
)
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|~|～|—|–|至|到)\s*")
_LIST_SPLIT_RE = re.compile(r"\s*(?:、|,|，|/|；|;)\s*")


class FloorSpec:
    __slots__ = ("raw", "status", "reason", "levels", "span", "is_range")

    def __init__(self, raw, status="ok", reason="", levels=None, span=0, is_range=False):
        self.raw = raw
        self.status = status
        self.reason = reason
        self.levels = levels or []
        self.span = span
        self.is_range = is_range

    @property
    def ok(self):
        return self.status == "ok" and bool(self.levels)

    def as_dict(self):
        return {
            "raw": self.raw,
            "status": self.status,
            "is_range": self.is_range,
            "span": self.span,
            "levels": [item["label"] for item in self.levels],
            **({"reason": self.reason} if self.reason else {}),
        }


def _parse_endpoint(text):
    match = _ENDPOINT_RE.match((text or "").strip())
    if not match:
        return None
    number = int(match.group("number"))
    if number == 0:
        return None
    if match.group("basement"):
        return {"level": -number, "label": f"B{number}"}
    return {"level": number, "label": f"{number}F"}


def _count_levels(start, end):
    low, high = sorted((start, end))
    count = high - low + 1
    if low <= 0 <= high:
        count -= 1  # 不存在 0 层
    return count


def parse_floor(text, maximum_span=12):
    raw = normalize_text(text)
    if not raw:
        return FloorSpec(raw, status="empty", reason="楼层为空")

    # 支持用顿号/逗号列出的多个单层，但不处理没有明确范围的省略写法。
    tokens = [t for t in _LIST_SPLIT_RE.split(raw) if t]
    if len(tokens) > 1:
        levels = []
        for token in tokens:
            endpoint = _parse_endpoint(token)
            if endpoint is None:
                return FloorSpec(raw, status="unparseable", reason=f"无法解析楼层“{token}”")
            levels.append(endpoint)
        return FloorSpec(raw, levels=levels, span=len(levels), is_range=True)

    parts = _RANGE_SPLIT_RE.split(raw)
    if len(parts) == 1:
        endpoint = _parse_endpoint(raw)
        if endpoint is None:
            return FloorSpec(raw, status="unparseable", reason="无法解析楼层")
        return FloorSpec(raw, levels=[endpoint], span=1, is_range=False)
    if len(parts) != 2:
        return FloorSpec(raw, status="unparseable", reason="楼层区间端点数量不正确")

    left, right = _parse_endpoint(parts[0]), _parse_endpoint(parts[1])
    if left is None or right is None:
        return FloorSpec(raw, status="unparseable", reason="楼层区间端点不合法")

    start, end = left["level"], right["level"]
    span = _count_levels(start, end)
    if span > maximum_span:
        return FloorSpec(
            raw, status="span_exceeded",
            reason=f"层段跨度 {span} 超过上限 {maximum_span}",
            span=span, is_range=True,
        )

    step = 1 if start <= end else -1
    levels = []
    for level in range(start, end + step, step):
        if level == 0:
            continue
        levels.append({
            "level": level,
            "label": f"B{abs(level)}" if level < 0 else f"{level}F",
        })
    return FloorSpec(raw, levels=levels, span=span, is_range=True)
