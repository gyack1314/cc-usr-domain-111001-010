"""结构化抽取：跨页继承、单位换算、楼层展开、脚注附着与隔离。

设计原则：

* 跨页沿用楼栋/楼层只允许同一 doc_id、同一 revision、同一 table-id，
  且下一页表格显式带 ``data-continue="true"``；普通新表不继承；
* 合并网格、表头、数量、楼层任何一环不可靠时，输出隔离记录而不是猜测；
* 每条记录都保留页码、table-id、原始 DOM 行列、继承路径与原文。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .dom import find, find_all, nearest_ancestor, normalize_text, outer_html, parse_html, text_content
from .floors import parse_floor
from .grid import Table
from .headers import map_headers
from .numbers import parse_quantity

DEFAULT_OPTIONS = {
    "maximum_floor_span": 12,
    "required_fields": ["building", "floor", "participants", "assembly_point"],
}

_GRID_INVALID_CODES = {"span_not_integer", "span_out_of_range", "rowspan_overflow", "grid_overlap"}


@dataclass
class PageInfo:
    page_number: int
    url: str
    sha256: str
    html: str
    root: object = None
    meta: dict = field(default_factory=dict)
    tables: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class Segment:
    table_id: str
    page: int
    revision: str
    doc_id: str
    valid_chain: bool = True
    invalid_reason: str = ""
    invalid_detail: str = ""
    chain: list = field(default_factory=list)
    last_building: dict = None
    last_floor: dict = None

    def chain_entry(self, continued):
        return {"page": self.page, "table_id": self.table_id, "data_continue": continued}


def _meta_of(root):
    node = find(root, lambda n: n.get("data-doc-id") is not None)
    if node is None:
        return {}, None
    return {
        "doc_id": (node.get("data-doc-id") or "").strip(),
        "revision": (node.get("data-revision") or "").strip(),
        "page": (node.get("data-page") or "").strip(),
    }, node


def _top_level_tables(scope):
    tables = []
    for node in find_all(scope, lambda n: n.tag == "table"):
        if nearest_ancestor(node, "table") is None:
            tables.append(node)
    return tables


def _cell_ref(page_number, table_id, cell):
    ref = cell.as_ref(page_number, table_id)
    return ref


def _header_ref(page_number, table_id, label):
    return {
        "page": page_number,
        "table_id": table_id,
        "row": label.row,
        "col": label.col,
        "text": label.text,
        "rowspan": label.rowspan,
        "colspan": label.colspan,
    }


def _row_raw_text(row):
    return " | ".join(cell.raw_text for cell in row.cells if cell.raw_text)


def _quarantine(quarantine, reason, message, page_number, table_id, row=None, cells=(), extra=None):
    entry = {
        "reason": reason,
        "message": message,
        "page": page_number,
        "table_id": table_id,
        "row": row,
        "cells": [c for c in cells if c],
    }
    if extra:
        entry.update(extra)
    quarantine.append(entry)
    return entry


def _collect_footnotes(page, table_id, row, page_root, quarantine):
    """收集行内 sup 引用并附着 aside 脚注；返回 (footnotes, conflict)。"""
    footnotes = []
    marker_to_ref = {}
    ref_to_marker = {}
    conflict_cells = []
    for cell in row.cells:
        for sup in find_all(cell.node, lambda n: n.tag == "sup", stop_at={"table"}):
            ref_id = (sup.get("data-ref") or "").strip()
            marker = normalize_text(text_content(sup, skip_tags=()))
            cell_ref = _cell_ref(page.page_number, table_id, cell)
            if not ref_id:
                _quarantine(
                    quarantine, "footnote_unresolved",
                    "脚注标记缺少 data-ref，无法建立引用关系",
                    page.page_number, table_id, row=row.index,
                    cells=[cell_ref], extra={"marker": marker},
                )
                continue
            if marker and marker in marker_to_ref and marker_to_ref[marker] != ref_id:
                conflict_cells.append(cell_ref)
            if ref_id in ref_to_marker and ref_to_marker[ref_id] != marker:
                conflict_cells.append(cell_ref)
            marker_to_ref.setdefault(marker, ref_id)
            ref_to_marker.setdefault(ref_id, marker)
            aside = find(page_root, lambda n: n.tag == "aside" and (n.get("id") or "") == ref_id)
            if aside is None:
                _quarantine(
                    quarantine, "footnote_unresolved",
                    f"找不到 id={ref_id} 的脚注内容",
                    page.page_number, table_id, row=row.index,
                    cells=[cell_ref], extra={"marker": marker, "ref": ref_id},
                )
                continue
            footnotes.append({
                "ref": ref_id,
                "marker": marker,
                "text": normalize_text(text_content(aside, skip_tags=())),
                "source": cell_ref,
                "aside_html": outer_html(aside),
            })
    return footnotes, conflict_cells


def _collect_nested_notes(page, table_id, row):
    notes = []
    for cell in row.cells:
        nested = find_all(cell.node, lambda n: n.tag == "table", stop_at={"table"})
        for table_node in nested:
            rows = []
            for tr in find_all(table_node, lambda n: n.tag == "tr", stop_at={"table"}):
                cells = [
                    normalize_text(text_content(c, skip_tags=("table", "sup")))
                    for c in tr.children if c.tag in {"td", "th"}
                ]
                if cells:
                    rows.append(cells)
            notes.append({
                "kind": "nested_remark_table",
                "page": page.page_number,
                "table_id": table_id,
                "row": cell.row,
                "col": cell.col,
                "rows": rows,
                "html": outer_html(table_node),
            })
    return notes


def extract_document(pages, doc_id, revision, options=None, collection_complete=True):
    """抽取一个文档某一修订的全部页面。

    ``pages`` 必须按来源给出的顺序传入；函数不会重排，也不会用其它修订
    的页面补缺。
    """
    options = {**DEFAULT_OPTIONS, **(options or {})}
    required = list(options.get("required_fields") or DEFAULT_OPTIONS["required_fields"])
    max_span = int(options.get("maximum_floor_span", DEFAULT_OPTIONS["maximum_floor_span"]))
    revision = str(revision)

    records = []
    quarantine = []
    footnotes_all = []
    notes_all = []
    version_errors = []
    page_infos = []

    # ---------------------------------------------------------- 页面级校验
    parsed_pages = []
    for index, page in enumerate(pages):
        info = PageInfo(
            page_number=page.page_number,
            url=page.url,
            sha256=page.sha256,
            html=page.html,
        )
        info.root = parse_html(page.html)
        meta, meta_node = _meta_of(info.root)
        info.meta = meta
        if meta_node is None:
            info.errors.append({"code": "page_metadata_missing", "message": "页面缺少 data-doc-id/data-revision/data-page"})
        else:
            if meta.get("doc_id") != doc_id:
                info.errors.append({"code": "page_doc_mismatch",
                                    "message": f"页面 doc_id={meta.get('doc_id')} 与文档 {doc_id} 不一致"})
            if meta.get("revision") != revision:
                info.errors.append({"code": "page_version_mismatch",
                                    "message": f"页面 revision={meta.get('revision')} 与文档修订 {revision} 不一致"})
            if meta.get("page") and meta["page"] != str(page.page_number):
                info.errors.append({"code": "page_metadata_mismatch",
                                    "message": f"页面 data-page={meta['page']} 与链接序号 {page.page_number} 不一致"})
        if page.page_number != index + 1:
            info.errors.append({"code": "page_order_invalid",
                                "message": f"第 {index + 1} 个链接指向第 {page.page_number} 页，顺序被交换或缺页"})
        scope = find(info.root, lambda n: n.tag == "main") or info.root
        for table_index, table_node in enumerate(_top_level_tables(scope), start=1):
            info.tables.append((table_index, Table(table_node, page.page_number, table_index)))
        parsed_pages.append(info)
        page_infos.append(info)
        version_errors.extend(info.errors)

    version_consistent = not any(
        e["code"] in {"page_doc_mismatch", "page_version_mismatch"} for e in version_errors
    )
    order_valid = not any(
        e["code"] in {"page_order_invalid", "page_metadata_mismatch", "page_metadata_missing"}
        for e in version_errors
    )

    # ---------------------------------------------------------- 表格与行
    previous_page_last_segment = None
    previous_page_number = None

    for info in parsed_pages:
        page_bad_version = any(
            e["code"] in {"page_doc_mismatch", "page_version_mismatch"} for e in info.errors
        )
        page_last_segment = None
        page_revision = info.meta.get("revision") or revision
        page_doc = info.meta.get("doc_id") or doc_id
        for table_index, table in info.tables:
            continued = (table.node.get("data-continue") or "").strip().lower() == "true"
            segment = None
            if continued:
                predecessor = previous_page_last_segment
                adjacent = previous_page_number is not None and previous_page_number == info.page_number - 1
                first_on_page = table_index == 1
                if (predecessor is not None and adjacent and first_on_page
                        and table.table_id
                        and predecessor.table_id == table.table_id
                        and predecessor.revision == revision
                        and predecessor.doc_id == doc_id
                        and page_revision == revision and page_doc == doc_id
                        and predecessor.valid_chain and not page_bad_version):
                    segment = Segment(
                        table_id=table.table_id, page=info.page_number,
                        revision=page_revision, doc_id=page_doc,
                        valid_chain=True,
                        chain=predecessor.chain + [{"page": info.page_number,
                                                    "table_id": table.table_id,
                                                    "data_continue": True}],
                        last_building=predecessor.last_building,
                        last_floor=predecessor.last_floor,
                    )
                else:
                    reason = "continuation_without_predecessor"
                    detail = "续页表没有可衔接的前一页同表"
                    if page_bad_version:
                        reason = "cross_version_continuation"
                        detail = "续页表所在页面与文档修订不一致"
                    elif predecessor is not None and predecessor.revision != revision:
                        reason = "cross_version_continuation"
                        detail = "续页表衔接了其它修订的页面"
                    elif predecessor is not None and predecessor.table_id != table.table_id:
                        reason = "continuation_table_mismatch"
                        detail = (f"data-continue=true 但前一页最后的表是 "
                                  f"{predecessor.table_id!r}，本表是 {table.table_id!r}")
                    elif predecessor is not None and not adjacent:
                        reason = "continuation_not_adjacent"
                        detail = "续页表不在紧接的下一页"
                    elif predecessor is not None and not first_on_page:
                        reason = "continuation_not_first"
                        detail = "续页表不是本页第一张表"
                    segment = Segment(
                        table_id=table.table_id or f"__missing_id_p{info.page_number}_t{table_index}",
                        page=info.page_number, revision=page_revision, doc_id=page_doc,
                        valid_chain=False, invalid_reason=reason, invalid_detail=detail,
                        chain=[{"page": info.page_number, "table_id": table.table_id,
                                "data_continue": True}],
                    )
            else:
                segment = Segment(
                    table_id=table.table_id or f"__missing_id_p{info.page_number}_t{table_index}",
                    page=info.page_number, revision=page_revision, doc_id=page_doc,
                    valid_chain=True,
                    chain=[{"page": info.page_number, "table_id": table.table_id,
                            "data_continue": False}],
                )

            _extract_table(
                info, table, segment, continued, page_bad_version,
                records, quarantine, footnotes_all, notes_all,
                required, max_span, doc_id, revision,
            )
            page_last_segment = segment

        previous_page_last_segment = page_last_segment
        previous_page_number = info.page_number

    publishable = (
        collection_complete
        and bool(parsed_pages)
        and version_consistent
        and order_valid
    )

    result = {
        "schema_version": "1.0",
        "document": {
            "doc_id": doc_id,
            "revision": revision,
            "publishable": publishable,
            "collection_complete": collection_complete,
            "version_consistent": version_consistent,
            "page_order_valid": order_valid,
            "version_errors": version_errors,
            "pages": [
                {
                    "page": info.page_number,
                    "url": info.url,
                    "sha256": info.sha256,
                    "doc_id": info.meta.get("doc_id"),
                    "revision": info.meta.get("revision"),
                    "data_page": info.meta.get("page"),
                    "tables": [
                        {
                            "table_id": table.table_id,
                            "data_continue": (table.node.get("data-continue") or "").strip().lower() == "true",
                            "rows": len(table.rows),
                            "header_rows": [r.index for r in table.header_rows],
                            "data_rows": [r.index for r in table.data_rows],
                        }
                        for _, table in info.tables
                    ],
                }
                for info in page_infos
            ],
        },
        "options": options,
        "records": records,
        "quarantine": quarantine,
        "footnotes": footnotes_all,
        "notes": notes_all,
        "statistics": {
            "records": len(records),
            "quarantine": len(quarantine),
            "quarantine_by_reason": _count_by_reason(quarantine),
            "footnotes": len(footnotes_all),
            "notes": len(notes_all),
        },
    }
    return result


def _count_by_reason(quarantine):
    counts = {}
    for entry in quarantine:
        counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
    return counts


def _extract_table(info, table, segment, continued, page_bad_version,
                   records, quarantine, footnotes_all, notes_all,
                   required, max_span, doc_id, revision):
    page_number = info.page_number
    table_id = segment.table_id

    def row_cells_refs(row):
        return [_cell_ref(page_number, table_id, c) for c in row.cells]

    # 版本不一致的页面：整页隔离，绝不与目标修订混排。
    if page_bad_version:
        for row in table.data_rows:
            _quarantine(
                quarantine, "cross_version_page",
                "页面修订与文档修订不一致，整页隔离",
                page_number, table_id, row=row.index, cells=row_cells_refs(row),
                extra={"raw_text": _row_raw_text(row)},
            )
        return

    # 续页衔接不合法：整段隔离。
    if not segment.valid_chain:
        for row in table.data_rows:
            _quarantine(
                quarantine, segment.invalid_reason, segment.invalid_detail,
                page_number, table_id, row=row.index, cells=row_cells_refs(row),
                extra={"raw_text": _row_raw_text(row), "continuation_chain": segment.chain},
            )
        return

    if not table.table_id:
        for row in table.data_rows:
            _quarantine(
                quarantine, "missing_table_id",
                "表格缺少 data-table-id，无法确认逻辑表归属",
                page_number, table_id, row=row.index, cells=row_cells_refs(row),
                extra={"raw_text": _row_raw_text(row)},
            )
        return

    # 合并网格问题。
    grid_invalid = [p for p in table.problems if p.code in _GRID_INVALID_CODES]
    numbering_conflicts = [p for p in table.problems if p.code == "row_numbering_conflict"]
    if grid_invalid:
        cells = []
        for problem in grid_invalid:
            cells.extend(_cell_ref(page_number, table_id, c) for c in problem.cells)
        for row in table.data_rows:
            _quarantine(
                quarantine, "merged_grid_invalid",
                "；".join(p.message for p in grid_invalid),
                page_number, table_id, row=row.index,
                cells=cells + row_cells_refs(row),
                extra={"raw_text": _row_raw_text(row),
                       "grid_problems": [{"code": p.code, "message": p.message} for p in grid_invalid]},
            )
        return
    conflict_rows = set()
    for problem in numbering_conflicts:
        for cell in problem.cells:
            conflict_rows.add(cell.row)

    # 表头语义。
    header_map = map_headers(table, required)
    if not header_map.ok:
        involved = [_header_ref(page_number, table_id, label) for label in header_map.involved]
        reason = {
            "header_ambiguous": "header_ambiguous",
            "header_missing": "header_missing",
            "participant_unit_ambiguous": "participant_unit_ambiguous",
        }.get(header_map.status, "header_ambiguous")
        for row in table.data_rows:
            _quarantine(
                quarantine, reason, header_map.reason,
                page_number, table_id, row=row.index,
                cells=involved + row_cells_refs(row),
                extra={"raw_text": _row_raw_text(row),
                       "header_paths": {str(col): table.column_header_text(col)
                                        for col in sorted(table.header_paths)}},
            )
        return

    for row in table.data_rows:
        if row.index in conflict_rows:
            cells = []
            for problem in numbering_conflicts:
                cells.extend(_cell_ref(page_number, table_id, c) for c in problem.cells)
            _quarantine(
                quarantine, "row_numbering_conflict",
                "；".join(p.message for p in numbering_conflicts),
                page_number, table_id, row=row.index, cells=cells,
                extra={"raw_text": _row_raw_text(row)},
            )
            continue
        _extract_row(
            info, table, segment, row, header_map,
            records, quarantine, footnotes_all, notes_all,
            max_span, doc_id, revision,
        )


def _resolve_field(table, row, col, segment, field_name, page_number, table_id):
    """取字段单元格；空值时按段内规则继承。返回 (value, cell, inheritance)。"""
    cell = table.cell_at(row.index, col) if col else None
    text = normalize_text(cell.text) if cell is not None else ""
    inheritance = None
    if not text:
        if field_name == "building":
            last = segment.last_building
        elif field_name == "floor":
            last = segment.last_floor
        else:
            last = None
        if last:
            text = last["value"]
            inheritance = {
                "field": field_name,
                "value": text,
                "source": last["ref"],
                "via": last["via"],
                "chain": segment.chain,
            }
            cell = None
    return text, cell, inheritance


def _extract_row(info, table, segment, row, header_map,
                 records, quarantine, footnotes_all, notes_all,
                 max_span, doc_id, revision):
    page_number = info.page_number
    table_id = segment.table_id

    def cell_ref(cell):
        return _cell_ref(page_number, table_id, cell)

    field_cells = {}
    field_texts = {}
    field_inheritance = {}
    missing = []
    for field_name, col in header_map.field_to_col.items():
        text, cell, inheritance = _resolve_field(
            table, row, col, segment, field_name, page_number, table_id)
        field_cells[field_name] = cell
        field_texts[field_name] = text
        field_inheritance[field_name] = inheritance
        if not text:
            missing.append(field_name)

    involved = [cell_ref(c) for c in field_cells.values() if c is not None]
    if not involved:
        involved = [cell_ref(c) for c in row.cells]

    if missing:
        _quarantine(
            quarantine, "required_field_missing",
            "缺少必需字段：" + "、".join(missing),
            page_number, table_id, row=row.index, cells=involved,
            extra={"raw_text": _row_raw_text(row), "missing_fields": missing},
        )
        return

    # 行内脚注编号冲突（同一编号指向不同脚注，或同一脚注编号不一致）。
    footnotes, conflict_cells = _collect_footnotes(info, table_id, row, info.root, quarantine)
    if conflict_cells:
        _quarantine(
            quarantine, "row_numbering_conflict",
            "行内脚注编号与引用关系冲突",
            page_number, table_id, row=row.index,
            cells=conflict_cells + involved,
            extra={"raw_text": _row_raw_text(row)},
        )
        return

    quantity = parse_quantity(field_texts["participants"], header_map.unit)
    if not quantity.ok:
        reason = {
            "inexact": "quantity_inexact",
            "unparseable": "quantity_unparseable",
            "ambiguous_unit": "participant_unit_ambiguous",
            "missing": "required_field_missing",
        }.get(quantity.status, "quantity_unparseable")
        _quarantine(
            quarantine, reason, quantity.reason or "数量不可用",
            page_number, table_id, row=row.index, cells=involved,
            extra={
                "raw_text": _row_raw_text(row),
                "quantity": quantity.as_dict(),
                "context": {
                    "building": field_texts.get("building"),
                    "floor": field_texts.get("floor"),
                    "assembly_point": field_texts.get("assembly_point"),
                },
            },
        )
        return

    floor_spec = parse_floor(field_texts["floor"], maximum_span=max_span)
    if not floor_spec.ok:
        reason = {
            "span_exceeded": "floor_span_exceeded",
            "empty": "required_field_missing",
        }.get(floor_spec.status, "floor_unparseable")
        _quarantine(
            quarantine, reason, floor_spec.reason or "楼层不可用",
            page_number, table_id, row=row.index, cells=involved,
            extra={"raw_text": _row_raw_text(row), "floor": floor_spec.as_dict()},
        )
        return

    notes = _collect_nested_notes(info, table_id, row)
    notes_all.extend(notes)
    footnotes_all.extend(
        {**fn, "row": row.index, "page": page_number, "table_id": table_id}
        for fn in footnotes
    )

    source_row_key = f"{doc_id}|r{revision}|{table_id}|p{page_number}|row{row.index}"
    raw_row_text = _row_raw_text(row)

    # 更新段内继承上下文（跨页只在合法续页链上生效）。
    def _field_ref(field_name):
        cell = field_cells[field_name]
        if cell is not None:
            via = "rowspan" if cell.row != row.index else "cell"
            return {"value": field_texts[field_name],
                    "ref": cell_ref(cell), "via": via}
        inh = field_inheritance[field_name]
        if inh:
            return {"value": inh["value"], "ref": inh["source"], "via": inh["via"]}
        return None

    building_ctx = _field_ref("building")
    floor_ctx = _field_ref("floor")
    if building_ctx:
        segment.last_building = building_ctx
    if floor_ctx:
        segment.last_floor = floor_ctx

    evidence_fields = {}
    for field_name in ("building", "floor", "participants", "assembly_point"):
        cell = field_cells[field_name]
        entry = {
            "value": field_texts[field_name],
            "logical_row": row.index,
            "logical_column": header_map.field_to_col[field_name],
        }
        if cell is not None:
            entry["cell"] = cell_ref(cell)
            entry["via_rowspan"] = cell.row != row.index
        if field_inheritance[field_name]:
            entry["inherited"] = True
            entry["inheritance"] = field_inheritance[field_name]
        evidence_fields[field_name] = entry

    for level in floor_spec.levels:
        record = {
            "record_id": f"{doc_id}-r{revision}-{table_id}-p{page_number}-row{row.index}-{level['label']}",
            "source_row_key": source_row_key,
            "doc_id": doc_id,
            "revision": revision,
            "table_id": table_id,
            "building": field_texts["building"],
            "floor": level["label"],
            "floor_level": level["level"],
            "participants": quantity.value,
            "participants_unit": "人",
            "assembly_point": field_texts["assembly_point"],
            "participants_scope": "row_record",
            "quantity": quantity.as_dict(),
            "floor_span": floor_spec.as_dict() if floor_spec.is_range else None,
            "shared_row_evidence": floor_spec.is_range,
            "evidence": {
                "page": page_number,
                "page_url": info.url,
                "page_sha256": info.sha256,
                "table_id": table_id,
                "row": row.index,
                "fields": evidence_fields,
                "row_raw_text": raw_row_text,
                "header_paths": {str(col): table.column_header_text(col)
                                 for col in sorted(table.header_paths)},
                "continuation_chain": segment.chain,
            },
            "footnotes": footnotes,
            "notes": notes,
        }
        records.append(record)
