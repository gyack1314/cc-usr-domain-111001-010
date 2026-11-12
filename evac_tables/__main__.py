"""命令行入口：文档链接、采集、抽取、发布与版本比较。

用法示例：

    python -m evac_tables list --base-url http://127.0.0.1:8090
    python -m evac_tables extract --base-url http://127.0.0.1:8090 \
        --doc-id D1 --state-dir state --output-dir out \
        --options sources/extraction-options.json
    python -m evac_tables extract-local --doc-id D1 --revision 1 \
        --state-dir state --output-dir out sources/r1-p1.html sources/r1-p2.html
    python -m evac_tables diff --state-dir state --doc-id D1 --from 1 --to 2
"""
from __future__ import annotations

import argparse
import json
import sys

from .collect import CollectionError, SourceClient, load_local_pages
from .extract import DEFAULT_OPTIONS, extract_document
from .publish import IncompleteVersionError, Publisher, atomic_write_json


def _load_options(path):
    if not path:
        return dict(DEFAULT_OPTIONS)
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    merged = dict(DEFAULT_OPTIONS)
    merged.update(data)
    return merged


def _emit(payload, stream=sys.stdout):
    stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _fail(payload, code=2):
    _emit(payload, stream=sys.stderr)
    return code


def _run_extract(collected, options, publisher):
    result = extract_document(
        collected.pages, collected.doc_id, collected.revision,
        options=options, collection_complete=True,
    )
    try:
        summary = publisher.publish(result, collected)
    except IncompleteVersionError as exc:
        failed_path = (
            publisher._doc_state(collected.doc_id) / "failed" / f"r{collected.revision}.json"
        )
        atomic_write_json(failed_path, result)
        return None, {
            "status": "rejected",
            "reason": str(exc),
            "failed_result": str(failed_path),
            "quarantine": result["statistics"]["quarantine_by_reason"],
        }
    return summary, None


def cmd_list(args):
    client = SourceClient(args.base_url, timeout=args.timeout)
    try:
        listing = client.list_documents()
    except CollectionError as exc:
        return _fail({"status": "error", "code": exc.code, "message": str(exc)})
    _emit({"status": "ok", **listing})
    return 0


def cmd_extract(args):
    options = _load_options(args.options)
    client = SourceClient(args.base_url, timeout=args.timeout)
    publisher = Publisher(args.state_dir, args.output_dir)
    try:
        collected = client.collect(doc_id=args.doc_id, revision=args.revision)
    except CollectionError as exc:
        return _fail({
            "status": "error", "code": exc.code, "message": str(exc),
            "page": exc.page, "url": exc.url,
            "note": "采集失败，未发布任何版本，也不会用旧修订页面补缺。",
        })
    summary, error = _run_extract(collected, options, publisher)
    if error:
        return _fail(error)
    _emit({"status": "ok", "links": collected.links, **summary,
           "diff_summary": _diff_brief(summary.get("diff"))})
    return 0


def cmd_extract_local(args):
    options = _load_options(args.options)
    publisher = Publisher(args.state_dir, args.output_dir)
    try:
        collected = load_local_pages(args.pages, args.doc_id, args.revision)
    except CollectionError as exc:
        return _fail({"status": "error", "code": exc.code, "message": str(exc)})
    summary, error = _run_extract(collected, options, publisher)
    if error:
        return _fail(error)
    _emit({"status": "ok", "links": collected.links, **summary,
           "diff_summary": _diff_brief(summary.get("diff"))})
    return 0


def _diff_brief(diff):
    if not diff:
        return None
    return {
        "from_revision": diff["from_revision"],
        "to_revision": diff["to_revision"],
        "added": len(diff["added"]),
        "removed": len(diff["removed"]),
        "changed": len(diff["changed"]),
        "unchanged": diff["unchanged"],
    }


def cmd_diff(args):
    publisher = Publisher(args.state_dir, args.output_dir or args.state_dir)
    try:
        diff = publisher.compare_revisions(args.doc_id, args.from_revision, args.to_revision)
    except FileNotFoundError as exc:
        return _fail({"status": "error", "message": str(exc)})
    _emit({"status": "ok", "diff": diff})
    return 0


def cmd_current(args):
    publisher = Publisher(args.state_dir, args.output_dir)
    current = publisher.load_current(args.doc_id)
    if current is None:
        return _fail({"status": "error", "message": f"没有已发布版本：{args.doc_id}"})
    _emit({"status": "ok", "result": current})
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="evac_tables", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="读取 /documents 文档链接入口")
    p_list.add_argument("--base-url", required=True)
    p_list.add_argument("--timeout", type=float, default=10)
    p_list.set_defaults(func=cmd_list)

    p_extract = sub.add_parser("extract", help="采集并发布一个完整修订")
    p_extract.add_argument("--base-url", required=True)
    p_extract.add_argument("--doc-id")
    p_extract.add_argument("--revision")
    p_extract.add_argument("--state-dir", required=True)
    p_extract.add_argument("--output-dir", required=True)
    p_extract.add_argument("--options")
    p_extract.add_argument("--timeout", type=float, default=10)
    p_extract.set_defaults(func=cmd_extract)

    p_local = sub.add_parser("extract-local", help="从本地 HTML 页面抽取（离线复核）")
    p_local.add_argument("--doc-id", required=True)
    p_local.add_argument("--revision", required=True)
    p_local.add_argument("--state-dir", required=True)
    p_local.add_argument("--output-dir", required=True)
    p_local.add_argument("--options")
    p_local.add_argument("pages", nargs="+")
    p_local.set_defaults(func=cmd_extract_local)

    p_diff = sub.add_parser("diff", help="逐字段比较两个归档修订")
    p_diff.add_argument("--state-dir", required=True)
    p_diff.add_argument("--output-dir")
    p_diff.add_argument("--doc-id", required=True)
    p_diff.add_argument("--from", dest="from_revision", required=True)
    p_diff.add_argument("--to", dest="to_revision", required=True)
    p_diff.set_defaults(func=cmd_diff)

    p_current = sub.add_parser("current", help="查看当前已发布版本")
    p_current.add_argument("--state-dir", required=True)
    p_current.add_argument("--output-dir", required=True)
    p_current.add_argument("--doc-id", required=True)
    p_current.set_defaults(func=cmd_current)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
