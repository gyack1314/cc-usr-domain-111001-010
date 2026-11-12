"""把 HTML ``table`` 转成带原始坐标的可校验网格。

关键约束：

* 只统计当前表格自己的行，嵌套表格的行永远不进入主体网格；
* ``rowspan``/``colspan`` 按 DOM 起点与跨度记录，证据坐标始终是原始
  ``tr``/``td`` 序号（从 1 开始）；
* 跨度越界、非法跨度、网格重叠或行内编号冲突都只登记问题，不猜测修复。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .dom import normalize_text, outer_html, text_content


def _with_span_attr(value):
    """返回 (span, problem_code or None)。"""
    if value is None or value == "":
        return 1, None
    try:
        span = int(str(value).strip())
    except (TypeError, ValueError):
        return 1, "span_not_integer"
    if span < 1:
        return 1, "span_out_of_range"
    return span, None


@dataclass
class Cell:
    node: object
    row: int                       # 原始 DOM 行序号，1 起
    col: int                       # 原始 DOM 该行内单元格序号，1 起
    rowspan: int = 1
    colspan: int = 1
    tag: str = ""
    text: str = ""                 # 主体文字：不含嵌套表、不含脚注标记
    raw_text: str = ""             # 含脚注标记，不含嵌套表
    html: str = ""

    def as_ref(self, page, table_id):
        return {
            "page": page,
            "table_id": table_id,
            "row": self.row,
            "col": self.col,
            "text": self.text,
            "raw_text": self.raw_text,
            "html": self.html,
            "rowspan": self.rowspan,
            "colspan": self.colspan,
        }


@dataclass
class Row:
    index: int
    section: str
    node: object = None
    cells: list[Cell] = field(default_factory=list)
    is_header: bool = False

    @property
    def all_th(self):
        return bool(self.cells) and all(cell.tag == "th" for cell in self.cells)


@dataclass
class HeaderLabel:
    text: str
    row: int
    col: int
    rowspan: int
    colspan: int

    def as_dict(self):
        return {
            "text": self.text,
            "row": self.row,
            "col": self.col,
            "rowspan": self.rowspan,
            "colspan": self.colspan,
        }


@dataclass
class GridProblem:
    code: str
    message: str
    cells: list[Cell] = field(default_factory=list)


class Table:
    """一张 ``data-table-id`` 表格的网格与表头。"""

    def __init__(self, node, page, table_index):
        self.node = node
        self.page = page
        self.table_index = table_index
        self.table_id = (node.get("data-table-id") or "").strip()
        self.rows: list[Row] = []
        self.header_rows: list[Row] = []
        self.data_rows: list[Row] = []
        self.occupancy: list[dict[int, Cell]] = []
        self.ncols = 0
        self.problems: list[GridProblem] = []
        self.header_paths: dict[int, list[HeaderLabel]] = {}
        self._collect_rows()
        self._build_grid()
        self._detect_row_id_conflicts()
        self._build_headers()

    # ------------------------------------------------------------------ rows
    def _collect_rows(self):
        def walk(parent, section):
            for child in parent.children:
                if child.tag is None:
                    continue
                if child.tag == "table":
                    # 嵌套备注表属于单元格内容，不是当前表的行。
                    continue
                if child.tag == "tr":
                    rows.append(Row(index=len(rows) + 1, section=section, node=child))
                    continue
                if child.tag in {"thead", "tbody", "tfoot"}:
                    walk(child, child.tag)
                else:
                    walk(child, section)

        rows = []
        walk(self.node, "tbody")
        self.rows = rows

        for row in self.rows:
            for cell_node in self._direct_cells(row.node):
                rowspan, rs_problem = _with_span_attr(cell_node.get("rowspan"))
                colspan, cs_problem = _with_span_attr(cell_node.get("colspan"))
                if rs_problem:
                    self.problems.append(GridProblem(
                        rs_problem, f"rowspan={cell_node.get('rowspan')!r}", []))
                if cs_problem:
                    self.problems.append(GridProblem(
                        cs_problem, f"colspan={cell_node.get('colspan')!r}", []))
                cell = Cell(
                    node=cell_node,
                    row=row.index,
                    col=len(row.cells) + 1,
                    rowspan=rowspan,
                    colspan=colspan,
                    tag=cell_node.tag,
                    text=text_content(cell_node, skip_tags=("table", "sup")),
                    raw_text=text_content(cell_node, skip_tags=("table",)),
                    html=outer_html(cell_node),
                )
                row.cells.append(cell)

    @staticmethod
    def _direct_cells(row_node):
        if row_node is None:
            return []
        return [child for child in row_node.children if child.tag in {"td", "th"}]

    # ------------------------------------------------------------------ grid
    def _build_grid(self):
        self.occupancy = [dict() for _ in self.rows]
        n_rows = len(self.rows)
        for row in self.rows:
            r = row.index - 1
            cursor = 0
            for cell in row.cells:
                while cursor in self.occupancy[r]:
                    cursor += 1
                cell.col = cursor + 1
                r_end = r + cell.rowspan - 1
                if r_end >= n_rows:
                    self.problems.append(GridProblem(
                        "rowspan_overflow",
                        f"rowspan={cell.rowspan} 超出表格行数 {n_rows}",
                        [cell],
                    ))
                    r_end = n_rows - 1
                c_end = cursor + cell.colspan - 1
                for rr in range(r, r_end + 1):
                    for cc in range(cursor, c_end + 1):
                        existing = self.occupancy[rr].get(cc)
                        if existing is not None and existing is not cell:
                            self.problems.append(GridProblem(
                                "grid_overlap",
                                f"单元格覆盖了已占用网格 ({rr + 1},{cc + 1})",
                                [existing, cell],
                            ))
                        else:
                            self.occupancy[rr][cc] = cell
                cursor = c_end + 1
                self.ncols = max(self.ncols, c_end + 1)

        # 行内编号属性冲突（同一行重复的非空编号）。
        for row in self.rows:
            seen = {}
            for cell in row.cells:
                for attr in ("data-cell-id", "id"):
                    value = (cell.node.get(attr) or "").strip()
                    if not value:
                        continue
                    key = (attr, value)
                    if key in seen:
                        self.problems.append(GridProblem(
                            "row_numbering_conflict",
                            f"{attr}={value!r} 在原始第 {row.index} 行重复",
                            [seen[key], cell],
                        ))
                    else:
                        seen[key] = cell

    def _detect_row_id_conflicts(self):
        # 检查行级 data-row-id 在同一表内重复（行内编号冲突的一种）。
        seen = {}
        for row in self.rows:
            value = (row.node.get("data-row-id") or "").strip() if row.node is not None else ""
            if not value:
                continue
            if value in seen:
                self.problems.append(GridProblem(
                    "row_numbering_conflict",
                    f"data-row-id={value!r} 在表中重复（原始第 {seen[value]} 行与本行）",
                    row.cells,
                ))
            else:
                seen[value] = row.index

    # --------------------------------------------------------------- headers
    def _build_headers(self):
        thead_rows = [row for row in self.rows if row.section == "thead" and row.cells]
        if thead_rows:
            self.header_rows = thead_rows
        else:
            leading = []
            for row in self.rows:
                if row.all_th:
                    leading.append(row)
                else:
                    break
            self.header_rows = leading
        header_indexes = {row.index for row in self.header_rows}
        self.data_rows = [row for row in self.rows if row.index not in header_indexes]

        self.header_paths = {}
        for row in self.header_rows:
            for cell in row.cells:
                for cc in range(cell.col, cell.col + cell.colspan):
                    self.header_paths.setdefault(cc, []).append(HeaderLabel(
                        text=cell.text, row=cell.row, col=cell.col,
                        rowspan=cell.rowspan, colspan=cell.colspan,
                    ))

    # ------------------------------------------------------------------ api
    @property
    def grid_problems(self):
        """合并网格/行内编号问题；不含表头语义问题。"""
        return list(self.problems)

    def cell_at(self, row_index, col):
        """返回覆盖 (行, 列) 的原始单元格。"""
        if row_index < 1 or row_index > len(self.occupancy):
            return None
        return self.occupancy[row_index - 1].get(col - 1)

    def column_header_text(self, col):
        labels = self.header_paths.get(col, [])
        return " / ".join(label.text for label in labels)

    def delete_problem(self):
        return any(p.code in {"span_not_integer", "span_out_of_range",
                              "rowspan_overflow", "grid_overlap"}
                    for p in self.problems)
