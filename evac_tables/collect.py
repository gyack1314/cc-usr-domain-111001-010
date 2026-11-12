"""采集：按 /documents 给出的顺序抓取页面，绝不跨修订拼页。

同一修订任意一页失败都会抛出 ``CollectionError``，调用方不得用旧修订
页面补缺；采集器也不会读取本地缓存或旧版本结果。
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .dom import find, parse_html


class CollectionError(RuntimeError):
    """采集失败：页面缺失、版本不一致或顺序异常。"""

    def __init__(self, message, code="collection_failed", page=None, url=None):
        super().__init__(message)
        self.code = code
        self.page = page
        self.url = url


@dataclass
class Page:
    page_number: int
    url: str
    html: str
    sha256: str


@dataclass
class CollectedDocument:
    doc_id: str
    revision: str
    pages: list = field(default_factory=list)
    links: dict = field(default_factory=dict)


def _page_metadata(html):
    root = parse_html(html)
    node = find(root, lambda n: n.get("data-doc-id") is not None)
    if node is None:
        return None
    return {
        "doc_id": (node.get("data-doc-id") or "").strip(),
        "revision": (node.get("data-revision") or "").strip(),
        "page": (node.get("data-page") or "").strip(),
    }


def _check_page(html, doc_id, revision, page_number, url):
    meta = _page_metadata(html)
    if meta is None:
        raise CollectionError(
            f"页面缺少 data-doc-id/data-revision/data-page：{url}",
            code="page_metadata_missing", page=page_number, url=url)
    if meta["doc_id"] != doc_id or meta["revision"] != str(revision):
        raise CollectionError(
            f"页面版本不一致（{meta['doc_id']}/r{meta['revision']}，期望 {doc_id}/r{revision}）：{url}",
            code="page_version_mismatch", page=page_number, url=url)
    if meta["page"] and meta["page"] != str(page_number):
        raise CollectionError(
            f"页面 data-page={meta['page']} 与链接序号 {page_number} 不一致：{url}",
            code="page_order_invalid", page=page_number, url=url)


class SourceClient:
    """来源 HTTP 客户端。"""

    def __init__(self, base_url, timeout=10):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def _get(self, url, expect_html=False):
        request = urllib.request.Request(url, headers={"Accept": "text/html,application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise CollectionError(
                f"读取失败 HTTP {exc.code}：{url}",
                code="http_error", url=url) from exc
        except urllib.error.URLError as exc:
            raise CollectionError(
                f"无法连接来源：{url}（{exc.reason}）",
                code="connection_error", url=url) from exc
        charset = "utf-8"
        match = re.search(r"charset=([\w.-]+)", content_type)
        if match:
            charset = match.group(1)
        return body, body.decode(charset, errors="replace"), content_type

    def list_documents(self):
        """读取文档入口，返回文档 id、修订与有序页面链接。"""
        url = urljoin(self.base_url, "documents")
        _, text, _ = self._get(url)
        payload = json.loads(text)
        documents = []
        for doc in payload.get("documents", []):
            pages = [urljoin(self.base_url, link) for link in doc.get("pages", [])]
            documents.append({
                "doc_id": doc.get("doc_id"),
                "revision": str(doc.get("revision")),
                "pages": pages,
            })
        return {"documents_url": url, "documents": documents}

    def collect(self, doc_id=None, revision=None):
        """按入口顺序抓取一个文档的某一修订；任何一页失败都整体失败。"""
        listing = self.list_documents()
        candidates = listing["documents"]
        if doc_id is not None:
            candidates = [d for d in candidates if d["doc_id"] == doc_id]
        if revision is not None:
            candidates = [d for d in candidates if d["revision"] == str(revision)]
        if not candidates:
            raise CollectionError(
                f"来源中没有文档 {doc_id or '*'} 修订 {revision or '*'}",
                code="document_not_found")
        if len(candidates) > 1:
            raise CollectionError(
                "存在多个候选文档，请显式指定 doc_id 与 revision",
                code="document_ambiguous")
        doc = candidates[0]
        pages = []
        for index, url in enumerate(doc["pages"], start=1):
            raw, html, _ = self._get(url, expect_html=True)
            _check_page(html, doc["doc_id"], doc["revision"], index, url)
            pages.append(Page(
                page_number=index, url=url, html=html,
                sha256=hashlib.sha256(raw).hexdigest(),
            ))
        return CollectedDocument(
            doc_id=doc["doc_id"], revision=doc["revision"], pages=pages,
            links={"documents": listing["documents_url"], "pages": doc["pages"]},
        )


def load_local_pages(paths, doc_id, revision):
    """从本地 HTML 文件构造文档（用于离线复核与演示）。

    文件顺序即页面顺序；元数据校验与在线采集一致。
    """
    pages = []
    for index, path in enumerate(paths, start=1):
        with open(path, "rb") as handle:
            raw = handle.read()
        html = raw.decode("utf-8", errors="replace")
        _check_page(html, doc_id, revision, index, str(path))
        pages.append(Page(
            page_number=index, url=str(path), html=html,
            sha256=hashlib.sha256(raw).hexdigest(),
        ))
    return CollectedDocument(
        doc_id=doc_id, revision=str(revision), pages=pages,
        links={"documents": None, "pages": [str(p) for p in paths]},
    )
