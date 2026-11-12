"""采集与结构化抽取的回归检查。

覆盖：跨页继承、单位换算、楼层展开、脚注附着、嵌套备注、隔离记录，
以及交换页序、插入无关备注列、新旧页混用三类防静默错配场景。
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from source_site.__main__ import running
from source_site.source import Source

from evac_tables.collect import CollectionError, Page, SourceClient, load_local_pages
from evac_tables.extract import DEFAULT_OPTIONS, extract_document
from evac_tables.floors import parse_floor
from evac_tables.numbers import parse_chinese_numeral, parse_quantity
from evac_tables.publish import IncompleteVersionError, Publisher

SOURCES = Path(__file__).resolve().parents[1] / "sources"
OPTIONS = json.loads((SOURCES / "extraction-options.json").read_text(encoding="utf-8"))


def make_page(html, number=1, url=None):
    raw = html.encode("utf-8")
    return Page(page_number=number, url=url or f"test://p{number}",
                html=html, sha256=hashlib.sha256(raw).hexdigest())


def local_pages(names, revision):
    return load_local_pages([str(SOURCES / n) for n in names], "D1", revision)


def extract(html_pages, doc_id="D1", revision="1", options=None):
    pages = [make_page(html, i + 1) for i, html in enumerate(html_pages)]
    return extract_document(pages, doc_id, revision, options=options or OPTIONS)


MAIN = '<main data-doc-id="D1" data-revision="{rev}" data-page="{page}">{body}</main>'


def main_html(body, rev=1, page=1):
    return MAIN.format(rev=rev, page=page, body=body)


def simple_table(rows, headers=("楼栋", "楼层", "人数", "集合点"), table_id="t", extra=""):
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f'<table data-table-id="{table_id}"{extra}><tr>{head}</tr>{body}</table>'


class NumberTests(unittest.TestCase):
    def test_chinese_numerals(self):
        self.assertEqual(2, parse_chinese_numeral("二"))
        self.assertEqual(10, parse_chinese_numeral("十"))
        self.assertEqual(15, parse_chinese_numeral("十五"))
        self.assertEqual(30, parse_chinese_numeral("三十"))
        self.assertEqual(23, parse_chinese_numeral("二十三"))
        self.assertEqual(100, parse_chinese_numeral("一百"))
        self.assertEqual(123, parse_chinese_numeral("一百二十三"))
        self.assertEqual(105, parse_chinese_numeral("一百零五"))
        self.assertEqual(200, parse_chinese_numeral("二百"))

    def test_chinese_numeral_rejects_illegal(self):
        for bad in ("", "二三", "十百", "一百一", "千", "三百百", "二十十"):
            self.assertIsNone(parse_chinese_numeral(bad), bad)

    def test_header_unit_hundred(self):
        q = parse_quantity("二", "hundred")
        self.assertTrue(q.ok)
        self.assertEqual(200, q.value)
        self.assertEqual("header", q.unit_source)

    def test_cell_unit_overrides_header(self):
        q = parse_quantity("30人", "hundred")
        self.assertEqual(30, q.value)
        self.assertEqual("cell", q.unit_source)
        q = parse_quantity("二百人", "person")
        self.assertEqual(200, q.value)

    def test_default_unit_is_person(self):
        q = parse_quantity("15", None)
        self.assertEqual(15, q.value)
        self.assertEqual("default", q.unit_source)

    def test_inexact_quantities_rejected(self):
        for text in ("约三十", "30多人", "多人", "十至二十", "30-40", "30人左右",
                     "三十余", "若干", "10~20", "30.5"):
            q = parse_quantity(text, "person")
            self.assertFalse(q.ok, text)
            self.assertEqual("inexact", q.status, text)
            self.assertIsNone(q.value)


class FloorTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(["1F", "2F", "3F"], parse_floor("1-3F").as_dict()["levels"])
        self.assertEqual(["B2", "B1"], parse_floor("B2-B1").as_dict()["levels"])
        self.assertEqual(["B1", "1F", "2F", "3F"], parse_floor("B1-3F").as_dict()["levels"])
        self.assertEqual(["2F"], parse_floor("2F").as_dict()["levels"])

    def test_span_limit(self):
        spec = parse_floor("1-3F", maximum_span=2)
        self.assertEqual("span_exceeded", spec.status)
        self.assertFalse(spec.ok)
        self.assertEqual(3, spec.span)

    def test_unparseable(self):
        self.assertEqual("unparseable", parse_floor("地上若干层").status)
        self.assertEqual("unparseable", parse_floor("B0").status)


class SampleExtractionTests(unittest.TestCase):
    """sources/ 合成文档：跨页继承、单位换算、楼层展开、脚注、隔离。"""

    def test_revision1_full(self):
        collected = local_pages(["r1-p1.html", "r1-p2.html"], 1)
        result = extract_document(collected.pages, "D1", "1", options=OPTIONS)
        self.assertTrue(result["document"]["publishable"])
        records = {(r["building"], r["floor"]): r for r in result["records"]}
        self.assertEqual(6, len(records))
        # 表头“人数（百人）”：二 -> 200 人
        self.assertEqual(200, records[("商场甲", "1F")]["participants"])
        self.assertEqual(200, records[("商场甲", "3F")]["participants"])
        # 单元格显式“30人”优先于表头百人
        self.assertEqual(30, records[("商场甲", "B2")]["participants"])
        self.assertEqual(30, records[("商场甲", "B1")]["participants"])
        # 新表（无 data-continue）不继承，办公乙独立成行
        self.assertEqual(15, records[("办公乙", "2F")]["participants"])
        self.assertEqual("西广场", records[("办公乙", "2F")]["assembly_point"])

    def test_revision1_evidence_points_to_dom_cells(self):
        collected = local_pages(["r1-p1.html", "r1-p2.html"], 1)
        result = extract_document(collected.pages, "D1", "1", options=OPTIONS)
        records = {(r["building"], r["floor"]): r for r in result["records"]}
        # 1-3F 拆分后共享原始行证据：p1 第 3 行
        rec = records[("商场甲", "2F")]
        self.assertEqual("D1|r1|routes|p1|row3", rec["source_row_key"])
        self.assertTrue(rec["shared_row_evidence"])
        self.assertEqual(3, rec["evidence"]["row"])
        self.assertEqual(1, rec["evidence"]["page"])
        # B2 行的楼栋来自第 3 行的 rowspan 单元格
        b2 = records[("商场甲", "B2")]
        building = b2["evidence"]["fields"]["building"]
        self.assertTrue(building["via_rowspan"])
        self.assertEqual(3, building["cell"]["row"])
        self.assertEqual(1, building["cell"]["col"])
        self.assertEqual(4, b2["evidence"]["row"])
        # 人数单元格原文可定位
        qty = b2["evidence"]["fields"]["participants"]
        self.assertEqual("30人", qty["cell"]["raw_text"])
        self.assertEqual(4, qty["cell"]["row"])
        self.assertEqual(3, qty["cell"]["col"])

    def test_revision1_footnote_and_nested_note(self):
        collected = local_pages(["r1-p1.html", "r1-p2.html"], 1)
        result = extract_document(collected.pages, "D1", "1", options=OPTIONS)
        records = {(r["building"], r["floor"]): r for r in result["records"]}
        # 脚注以引用关系附着到引用行（拆分的每个楼层都附着）
        for floor in ("1F", "2F", "3F"):
            fns = records[("商场甲", floor)]["footnotes"]
            self.assertEqual(1, len(fns))
            self.assertEqual("f1", fns[0]["ref"])
            self.assertEqual("雨天改室内连廊", fns[0]["text"])
            self.assertEqual("东广场", records[("商场甲", floor)]["assembly_point"])
        # 嵌套备注表不进入主体字段
        b1 = records[("商场甲", "B1")]
        self.assertEqual("南门", b1["assembly_point"])
        self.assertNotIn("人工引导", b1["assembly_point"])
        notes = b1["notes"]
        self.assertEqual(1, len(notes))
        self.assertEqual([["备注", "人工引导"]], notes[0]["rows"])
        self.assertEqual(4, notes[0]["row"])
        self.assertEqual(4, notes[0]["col"])

    def test_revision1_inexact_quarantined(self):
        collected = local_pages(["r1-p1.html", "r1-p2.html"], 1)
        result = extract_document(collected.pages, "D1", "1", options=OPTIONS)
        self.assertFalse(any(r["floor"] == "4F" for r in result["records"]))
        hits = [q for q in result["quarantine"] if q["reason"] == "quantity_inexact"]
        self.assertEqual(1, len(hits))
        self.assertEqual(2, hits[0]["page"])
        self.assertEqual("routes", hits[0]["table_id"])
        cells = {(c["row"], c["col"]) for c in hits[0]["cells"]}
        self.assertIn((2, 3), cells)  # “约三十”所在单元格

    def test_revision2_cross_page_inheritance(self):
        collected = local_pages(["r2-p1.html", "r2-p2.html"], 2)
        result = extract_document(collected.pages, "D1", "2", options=OPTIONS)
        self.assertTrue(result["document"]["publishable"])
        records = {(r["building"], r["floor"]): r for r in result["records"]}
        self.assertEqual(2, len(records))
        rec = records[("商场甲", "2F")]
        self.assertEqual(10, rec["participants"])
        building = rec["evidence"]["fields"]["building"]
        self.assertTrue(building["inherited"])
        source = building["inheritance"]["source"]
        self.assertEqual((1, "routes", 2, 1),
                         (source["page"], source["table_id"], source["row"], source["col"]))
        chain = rec["evidence"]["continuation_chain"]
        self.assertEqual([1, 2], [c["page"] for c in chain])
        self.assertTrue(chain[1]["data_continue"])


class ContinuationGuardTests(unittest.TestCase):
    """交换页序、新旧页混用、跨版本续页：不得静默错配。"""

    def test_swapped_pages_not_publishable_and_no_leak(self):
        pages = [
            make_page((SOURCES / "r1-p2.html").read_text(encoding="utf-8"), 2),
            make_page((SOURCES / "r1-p1.html").read_text(encoding="utf-8"), 1),
        ]
        result = extract_document(pages, "D1", "1", options=OPTIONS)
        self.assertFalse(result["document"]["publishable"])
        self.assertIn("page_order_invalid",
                      {e["code"] for e in result["document"]["version_errors"]})
        # 4F 没有被归到办公乙或任何楼栋
        self.assertFalse(any(r["floor"] == "4F" for r in result["records"]))
        reasons = [q["reason"] for q in result["quarantine"]]
        self.assertIn("continuation_without_predecessor", reasons)

    def test_mixed_revisions_rejected_at_collection(self):
        with self.assertRaises(CollectionError) as ctx:
            load_local_pages([str(SOURCES / "r2-p1.html"), str(SOURCES / "r1-p2.html")], "D1", 2)
        self.assertEqual("page_version_mismatch", ctx.exception.code)

    def test_mixed_revisions_quarantined_at_extraction(self):
        pages = [
            make_page((SOURCES / "r2-p1.html").read_text(encoding="utf-8"), 1),
            make_page((SOURCES / "r1-p2.html").read_text(encoding="utf-8"), 2),
        ]
        result = extract_document(pages, "D1", "2", options=OPTIONS)
        self.assertFalse(result["document"]["publishable"])
        self.assertFalse(result["document"]["version_consistent"])
        # 旧修订页面整页隔离，不参与新修订记录
        self.assertEqual(1, len(result["records"]))
        self.assertEqual("1F", result["records"][0]["floor"])
        self.assertIn("cross_version_page", {q["reason"] for q in result["quarantine"]})

    def test_cross_version_continuation_flag(self):
        # r2p2 带 data-continue=true，但前一页是 r1：续页链跨版本
        pages = [
            make_page((SOURCES / "r1-p1.html").read_text(encoding="utf-8"), 1),
            make_page((SOURCES / "r2-p2.html").read_text(encoding="utf-8"), 2),
        ]
        result = extract_document(pages, "D1", "2", options=OPTIONS)
        self.assertFalse(result["document"]["publishable"])
        self.assertIn("cross_version_continuation", {q["reason"] for q in result["quarantine"]})
        self.assertFalse(any(r["floor"] == "2F" for r in result["records"]))

    def test_new_table_does_not_inherit(self):
        # 同一页上的新表（无 data-continue）不得沿用上一张表的楼栋
        html = main_html(
            simple_table([("商场甲", "1F", "10", "东")], table_id="a")
            + simple_table([("", "2F", "20", "南")], table_id="b")
        )
        result = extract([html])
        self.assertFalse(any(r["table_id"] == "b" for r in result["records"]))
        hits = [q for q in result["quarantine"] if q["table_id"] == "b"]
        self.assertTrue(hits)
        self.assertEqual("required_field_missing", hits[0]["reason"])

    def test_continuation_requires_flag(self):
        # 下一页同 table-id 但没有 data-continue=true：不得跨页继承
        p1 = main_html(simple_table([("商场甲", "1F", "10", "东")], table_id="t"), page=1)
        p2 = main_html(simple_table([("", "2F", "20", "南")], table_id="t"), page=2)
        result = extract([p1, p2])
        self.assertEqual(1, len(result["records"]))
        self.assertFalse(any(r["floor"] == "2F" for r in result["records"]))

    def test_continuation_table_mismatch(self):
        p1 = main_html(simple_table([("商场甲", "1F", "10", "东")], table_id="a"), page=1)
        p2 = main_html(
            simple_table([("", "2F", "20", "南")], table_id="b", extra=' data-continue="true"'),
            page=2)
        result = extract([p1, p2])
        self.assertIn("continuation_table_mismatch", {q["reason"] for q in result["quarantine"]})


class GridAndHeaderTests(unittest.TestCase):
    def test_rowspan_overflow_quarantined(self):
        html = main_html(
            '<table data-table-id="t"><tr><th>楼栋</th><th>楼层</th><th>人数</th><th>集合点</th></tr>'
            '<tr><td rowspan="3">商场甲</td><td>1F</td><td>10</td><td>东</td></tr>'
            '<tr><td>2F</td><td>20</td><td>南</td></tr></table>')
        result = extract([html])
        self.assertEqual(0, len(result["records"]))
        reasons = {q["reason"] for q in result["quarantine"]}
        self.assertIn("merged_grid_invalid", reasons)
        cells = [c for q in result["quarantine"] for c in q["cells"]]
        self.assertTrue(any(c["rowspan"] == 3 for c in cells))

    def test_ambiguous_header_quarantined(self):
        html = main_html(simple_table(
            [("商场甲", "1F", "10", "20", "东")],
            headers=("楼栋", "楼层", "人数", "人数", "集合点")))
        result = extract([html])
        self.assertEqual(0, len(result["records"]))
        self.assertIn("header_ambiguous", {q["reason"] for q in result["quarantine"]})

    def test_missing_required_column_quarantined(self):
        html = main_html(simple_table([("商场甲", "1F", "10")],
                                      headers=("楼栋", "楼层", "人数")))
        result = extract([html])
        self.assertEqual(0, len(result["records"]))
        self.assertIn("header_missing", {q["reason"] for q in result["quarantine"]})

    def test_unrelated_remark_column_does_not_shift_mapping(self):
        # 在楼层与人数之间插入无关备注列：字段仍按表头语义定位
        html = main_html(simple_table(
            [("商场甲", "1-2F", "无关备注", "二", "东")],
            headers=("楼栋", "楼层", "现场备注", "人数（百人）", "集合点")))
        result = extract([html])
        self.assertEqual(2, len(result["records"]))
        for rec in result["records"]:
            self.assertEqual(200, rec["participants"])
            self.assertEqual("东", rec["assembly_point"])
            qty = rec["evidence"]["fields"]["participants"]
            self.assertEqual(4, qty["logical_column"])

    def test_column_order_permutation(self):
        html = main_html(simple_table(
            [("东", "10", "1F", "商场甲")],
            headers=("集合点", "人数", "楼层", "楼栋")))
        result = extract([html])
        self.assertEqual(1, len(result["records"]))
        rec = result["records"][0]
        self.assertEqual(("商场甲", "1F", 10, "东"),
                         (rec["building"], rec["floor"], rec["participants"], rec["assembly_point"]))

    def test_row_numbering_conflict(self):
        html = main_html(
            '<table data-table-id="t"><tr><th>楼栋</th><th>楼层</th><th>人数</th><th>集合点</th></tr>'
            '<tr><td data-cell-id="c1">商场甲</td><td data-cell-id="c1">1F</td>'
            '<td>10</td><td>东</td></tr></table>')
        result = extract([html])
        self.assertEqual(0, len(result["records"]))
        self.assertIn("row_numbering_conflict", {q["reason"] for q in result["quarantine"]})

    def test_footnote_marker_conflict(self):
        html = main_html(
            '<table data-table-id="t"><tr><th>楼栋</th><th>楼层</th><th>人数</th><th>集合点</th></tr>'
            '<tr><td>商场甲</td><td>1F</td><td>10</td>'
            '<td>东<sup data-ref="f1">1</sup><sup data-ref="f2">1</sup></td></tr></table>'
            '<aside id="f1">甲</aside><aside id="f2">乙</aside>')
        result = extract([html])
        self.assertEqual(0, len(result["records"]))
        self.assertIn("row_numbering_conflict", {q["reason"] for q in result["quarantine"]})

    def test_floor_span_exceeded_not_truncated(self):
        options = dict(OPTIONS, maximum_floor_span=2)
        html = main_html(simple_table([("商场甲", "1-3F", "10", "东")]))
        result = extract([html], options=options)
        self.assertEqual(0, len(result["records"]))
        hits = [q for q in result["quarantine"] if q["reason"] == "floor_span_exceeded"]
        self.assertEqual(1, len(hits))
        self.assertEqual(3, hits[0]["floor"]["span"])


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evac-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.publisher = Publisher(self.tmp / "state", self.tmp / "out")

    def _collect_extract(self, names, revision):
        collected = local_pages(names, revision)
        result = extract_document(collected.pages, "D1", str(revision), options=OPTIONS)
        return collected, result

    def test_publish_keeps_only_new_version_downstream(self):
        c1, r1 = self._collect_extract(["r1-p1.html", "r1-p2.html"], 1)
        self.publisher.publish(r1, c1)
        c2, r2 = self._collect_extract(["r2-p1.html", "r2-p2.html"], 2)
        summary = self.publisher.publish(r2, c2)
        self.assertEqual("1", summary["previous_revision"])

        current = json.loads((self.tmp / "out" / "D1" / "current.json").read_text(encoding="utf-8"))
        self.assertEqual("2", current["document"]["revision"])
        self.assertEqual(2, len(current["records"]))
        self.assertFalse(any(r["table_id"] == "office" for r in current["records"]))

        archived = json.loads(
            (self.tmp / "state" / "D1" / "revisions" / "r1" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual("1", archived["document"]["revision"])
        self.assertEqual(6, len(archived["records"]))
        self.assertTrue((self.tmp / "state" / "D1" / "revisions" / "r1" / "pages" / "p1.html").is_file())

    def test_field_level_diff(self):
        c1, r1 = self._collect_extract(["r1-p1.html", "r1-p2.html"], 1)
        self.publisher.publish(r1, c1)
        c2, r2 = self._collect_extract(["r2-p1.html", "r2-p2.html"], 2)
        self.publisher.publish(r2, c2)
        diff = json.loads(
            (self.tmp / "state" / "D1" / "diffs" / "r1_to_r2.json").read_text(encoding="utf-8"))
        changed = {tuple(c["key"]): c["changes"] for c in diff["changed"]}
        self.assertEqual(200, changed[("routes", "商场甲", "1F")]["participants"]["old"])
        self.assertEqual(20, changed[("routes", "商场甲", "1F")]["participants"]["new"])
        self.assertEqual("东广场", changed[("routes", "商场甲", "1F")]["assembly_point"]["old"])
        self.assertEqual("新东广场", changed[("routes", "商场甲", "1F")]["assembly_point"]["new"])
        removed_keys = {tuple(r["key"]) for r in diff["removed"]}
        self.assertIn(("office", "办公乙", "2F"), removed_keys)
        self.assertIn(("routes", "商场甲", "B2"), removed_keys)

    def test_incomplete_version_not_published(self):
        pages = [
            make_page((SOURCES / "r2-p1.html").read_text(encoding="utf-8"), 1),
            make_page((SOURCES / "r1-p2.html").read_text(encoding="utf-8"), 2),
        ]
        result = extract_document(pages, "D1", "2", options=OPTIONS)
        with self.assertRaises(IncompleteVersionError):
            self.publisher.publish(result)
        self.assertFalse((self.tmp / "out" / "D1" / "current.json").exists())

    def test_failed_collection_preserves_old_current(self):
        c1, r1 = self._collect_extract(["r1-p1.html", "r1-p2.html"], 1)
        self.publisher.publish(r1, c1)
        before = (self.tmp / "out" / "D1" / "current.json").read_bytes()
        # 模拟同修订采集失败：没有新结果可发布，旧版本必须原样保留
        bad = extract_document(
            [make_page((SOURCES / "r2-p1.html").read_text(encoding="utf-8"), 1)],
            "D1", "2", options=OPTIONS, collection_complete=False)
        with self.assertRaises(IncompleteVersionError):
            self.publisher.publish(bad)
        self.assertEqual(before, (self.tmp / "out" / "D1" / "current.json").read_bytes())


class HttpSourceTests(unittest.TestCase):
    """通过真实本地 HTTP 来源验证采集行为。"""

    def test_list_documents_entry(self):
        with running() as (base, _source):
            client = SourceClient(base)
            listing = client.list_documents()
            doc = listing["documents"][0]
            self.assertEqual("D1", doc["doc_id"])
            self.assertEqual(2, len(doc["pages"]))
            self.assertTrue(doc["pages"][0].endswith("/doc/D1/r1/p1"))

    def test_collect_base_revision(self):
        with running() as (base, _source):
            collected = SourceClient(base).collect(doc_id="D1")
            self.assertEqual("1", collected.revision)
            self.assertEqual(2, len(collected.pages))
            result = extract_document(collected.pages, "D1", "1", options=OPTIONS)
            self.assertTrue(result["document"]["publishable"])
            self.assertEqual(6, len(result["records"]))

    def test_collect_revised_revision(self):
        source = Source()
        source.phase = "revised"
        with running(source) as (base, _source):
            collected = SourceClient(base).collect(doc_id="D1")
            self.assertEqual("2", collected.revision)
            result = extract_document(collected.pages, "D1", "2", options=OPTIONS)
            self.assertEqual(2, len(result["records"]))

    def test_partial_phase_fails_without_fallback(self):
        source = Source()
        source.phase = "partial"
        with running(source) as (base, _source):
            with self.assertRaises(CollectionError) as ctx:
                SourceClient(base).collect(doc_id="D1")
            self.assertEqual("http_error", ctx.exception.code)

    def test_http_404(self):
        with running() as (base, _source):
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError):
                urlopen(base + "/doc/D1/r9/p9", timeout=3)


if __name__ == "__main__":
    unittest.main()
