"""发布与版本比较。

* 只有 ``publishable`` 的完整版本才会写入下游 ``current.json``；
* 旧版本结果与原始页面保留在 ``state`` 归档中，用于逐字段比较；
* 所有写入先落临时文件再原子替换，失败不会留下半个版本。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


class IncompleteVersionError(RuntimeError):
    """版本不完整或校验未通过，禁止发布。"""


def _canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(data):
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def record_key(record):
    """跨修订稳定键：逻辑表 + 楼栋 + 楼层。"""
    return (record.get("table_id"), record.get("building"), record.get("floor"))


def _key_map(records):
    grouped = {}
    for record in records:
        grouped.setdefault(record_key(record), []).append(record)
    keyed = {}
    for key, group in grouped.items():
        group = sorted(group, key=lambda r: r.get("record_id", ""))
        for index, record in enumerate(group):
            keyed[(key, index)] = record
    return keyed


_COMPARE_FIELDS = ("building", "floor", "participants", "assembly_point")


def _record_projection(record):
    data = {field: record.get(field) for field in _COMPARE_FIELDS}
    data["footnotes"] = sorted(fn.get("text", "") for fn in record.get("footnotes", []))
    data["notes"] = sorted(
        "|".join("|".join(row) for row in note.get("rows", []))
        for note in record.get("notes", [])
    )
    return data


def compare_results(old, new):
    """逐字段比较两个完整版本的结构化结果。"""
    old_map = _key_map(old.get("records", []))
    new_map = _key_map(new.get("records", []))
    added, removed, changed, unchanged = [], [], [], 0
    for key in sorted(new_map, key=str):
        if key not in old_map:
            added.append({"key": list(key[0]), "record": new_map[key]})
    for key in sorted(old_map, key=str):
        if key not in new_map:
            removed.append({"key": list(key[0]), "record": old_map[key]})
    for key in sorted(old_map, key=str):
        if key not in new_map:
            continue
        before, after = _record_projection(old_map[key]), _record_projection(new_map[key])
        field_diffs = {
            field: {"old": before[field], "new": after[field]}
            for field in before if before[field] != after[field]
        }
        if field_diffs:
            changed.append({
                "key": list(key[0]),
                "changes": field_diffs,
                "old_record_id": old_map[key].get("record_id"),
                "new_record_id": new_map[key].get("record_id"),
            })
        else:
            unchanged += 1
    return {
        "doc_id": new.get("document", {}).get("doc_id"),
        "from_revision": old.get("document", {}).get("revision"),
        "to_revision": new.get("document", {}).get("revision"),
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "quarantine_old": old.get("statistics", {}).get("quarantine_by_reason", {}),
        "quarantine_new": new.get("statistics", {}).get("quarantine_by_reason", {}),
    }


class Publisher:
    def __init__(self, state_dir, output_dir):
        self.state_dir = Path(state_dir)
        self.output_dir = Path(output_dir)

    # ------------------------------------------------------------- paths
    def _doc_state(self, doc_id):
        return self.state_dir / doc_id

    def current_path(self, doc_id):
        return self.output_dir / doc_id / "current.json"

    def revision_dir(self, doc_id, revision):
        return self._doc_state(doc_id) / "revisions" / f"r{revision}"

    def diff_path(self, doc_id, old_revision, new_revision):
        return self._doc_state(doc_id) / "diffs" / f"r{old_revision}_to_r{new_revision}.json"

    # -------------------------------------------------------------- read
    def load_current(self, doc_id):
        path = self.current_path(doc_id)
        return read_json(path) if path.is_file() else None

    def load_revision(self, doc_id, revision):
        path = self.revision_dir(doc_id, revision) / "result.json"
        return read_json(path) if path.is_file() else None

    # ----------------------------------------------------------- publish
    def publish(self, result, collected=None):
        document = result.get("document", {})
        if not document.get("publishable"):
            raise IncompleteVersionError(
                "版本不完整或校验未通过，拒绝发布："
                + json.dumps(document.get("version_errors", []), ensure_ascii=False))
        doc_id = document["doc_id"]
        revision = str(document["revision"])

        previous = self.load_current(doc_id)
        diff = None
        if previous is not None:
            old_revision = str(previous.get("document", {}).get("revision"))
            self._archive(previous, collected_pages=None, revision=old_revision)
            if old_revision != revision:
                diff = compare_results(previous, result)
                atomic_write_json(self.diff_path(doc_id, old_revision, revision), diff)

        self._archive(result, collected_pages=collected, revision=revision)
        atomic_write_json(self.current_path(doc_id), result)
        return {
            "doc_id": doc_id,
            "revision": revision,
            "current": str(self.current_path(doc_id)),
            "archived": str(self.revision_dir(doc_id, revision)),
            "diff": diff,
            "previous_revision": str(previous.get("document", {}).get("revision")) if previous else None,
        }

    def _archive(self, result, collected_pages, revision):
        document = result.get("document", {})
        doc_id = document["doc_id"]
        target = self.revision_dir(doc_id, revision)
        target.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target / "result.json", result)
        pages_meta = []
        if collected_pages is not None:
            pages_dir = target / "pages"
            for page in collected_pages.pages:
                atomic_write_bytes(
                    pages_dir / f"p{page.page_number}.html",
                    page.html.encode("utf-8"),
                )
                pages_meta.append({
                    "page": page.page_number, "url": page.url, "sha256": page.sha256,
                })
        else:
            pages_meta = document.get("pages", [])
        atomic_write_json(target / "manifest.json", {
            "doc_id": doc_id,
            "revision": revision,
            "result_sha256": content_sha256(result),
            "pages": pages_meta,
        })

    def compare_revisions(self, doc_id, old_revision, new_revision):
        old = self.load_revision(doc_id, old_revision)
        new = self.load_revision(doc_id, new_revision)
        if old is None or new is None:
            raise FileNotFoundError(
                f"缺少归档版本：{doc_id} r{old_revision} 或 r{new_revision}")
        return compare_results(old, new)
