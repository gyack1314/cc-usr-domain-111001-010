"""按表头语义定位字段，不依赖固定列号。

多行列头会保留完整路径（如“疏散安排 / 人数（百人）”），字段匹配以叶子
标签为主。两列同时匹配同一字段、一个标签同时匹配多个字段或缺少必需列时，
该表整体隔离，绝不猜测列序。
"""
from __future__ import annotations

import re

from .dom import normalize_text

_FIELD_RULES = {
    "building": (
        lambda t: t in {"楼栋", "建筑", "楼号", "楼", "建筑物"} or "楼栋" in t or "建筑" in t,
    ),
    "floor": (
        lambda t: t in {"楼层", "层", "层号", "楼层号"} or "楼层" in t or "层数" in t,
    ),
    "participants": (
        lambda t: "人数" in t or "人员数" in t or "参加人数" in t or "疏散人数" in t,
    ),
    "assembly_point": (
        lambda t: "集合点" in t or "集合地点" in t or "疏散集合点" in t
        or t in {"集合", "疏散集合"},
    ),
}

_FIELD_ORDER = ("building", "floor", "participants", "assembly_point")

_PARENS = re.compile(r"[（(][^）)]*[）)]")


def header_topic(text):
    """去掉括号里的单位说明与空白，得到字段语义。"""
    text = _PARENS.sub("", normalize_text(text))
    return re.sub(r"[\s：:]+", "", text)


def _matches(topic):
    return [field for field in _FIELD_ORDER
            if any(rule(topic) for rule in _FIELD_RULES[field])]


class HeaderMap:
    __slots__ = ("field_to_col", "col_to_field", "unit", "unit_evidence",
                 "status", "reason", "involved")

    def __init__(self):
        self.field_to_col = {}
        self.col_to_field = {}
        self.unit = None
        self.unit_evidence = []
        self.status = "ok"
        self.reason = ""
        self.involved = []

    @property
    def ok(self):
        return self.status == "ok"

    def as_dict(self):
        return {
            "status": self.status,
            "fields": {
                field: {
                    "logical_column": col,
                    "header_path": self.involved_paths.get(col, "") if hasattr(self, "involved_paths") else "",
                }
                for field, col in self.field_to_col.items()
            },
            "participant_unit": self.unit,
            "unit_evidence": self.unit_evidence,
            **({"reason": self.reason} if self.reason else {}),
        }


def map_headers(table, required_fields):
    """解析 ``Table`` 的表头，返回 ``HeaderMap``。

    隔离原因中的 ``involved`` 是表头单元格原始坐标，便于资料员回到页面核看。
    """
    result = HeaderMap()
    if not table.header_rows:
        result.status = "header_missing"
        result.reason = "没有可识别的表头行"
        return result

    col_candidates = {}
    for col, labels in sorted(table.header_paths.items()):
        if not labels:
            continue
        leaf = labels[-1]
        topic = header_topic(leaf.text)
        matches = _matches(topic)
        if len(matches) > 1:
            result.status = "header_ambiguous"
            result.reason = f"列 {col} 的表头“{leaf.text}”同时匹配多个字段"
            result.involved.append(leaf)
            return result
        if matches:
            col_candidates[col] = matches[0]

    # 同一字段出现在多列，无法确定使用哪一列。
    by_field = {}
    for col, field in col_candidates.items():
        by_field.setdefault(field, []).append(col)
    for field, cols in by_field.items():
        if len(cols) > 1:
            result.status = "header_ambiguous"
            result.reason = f"字段 {field} 出现在多列：{cols}"
            for col in cols:
                result.involved.extend(table.header_paths.get(col, []))
            return result

    missing = [field for field in required_fields if field not in by_field]
    if missing:
        result.status = "header_missing"
        result.reason = "缺少必需列：" + "、".join(missing)
        result.involved.extend(
            label for labels in table.header_paths.values() for label in labels
        )
        return result

    result.field_to_col = {field: cols[0] for field, cols in by_field.items()}
    result.col_to_field = {col: field for field, col in result.field_to_col.items()}

    from .numbers import header_unit
    unit, evidence = header_unit(table.header_paths.get(result.field_to_col["participants"], []))
    result.unit = unit
    result.unit_evidence = evidence
    if unit == "ambiguous":
        result.status = "participant_unit_ambiguous"
        result.reason = "人数表头同时出现“人”和“百人”"
        result.involved.extend(table.header_paths.get(result.field_to_col["participants"], []))
        return result
    return result
