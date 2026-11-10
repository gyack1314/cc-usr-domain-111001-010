"""合成来源行为，可直接调用或通过本地 HTTP 访问。"""
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs

DATA = Path(__file__).resolve().parents[1] / "sources"


def json_file(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class Source:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []
        self.phase = "base"
        self.state = {}

    def request(self, method, path, query=None, headers=None, body=b""):
        query, headers = query or {}, headers or {}
        self.calls.append({"method": method, "path": path, "query": query})
        if path == "/__control" and method == "POST":
            control = json.loads(body or b"{}")
            self.phase = control.get("phase", self.phase)
            self.state.update(control.get("state", {}))
            return 200, {}, {"phase": self.phase}
        if path == "/__calls":
            return 200, {}, self.calls[:-1]
        return self.dispatch(method, path, query, headers, body)

    def dispatch(self, method, path, q, h, body):
        revision = 2 if self.phase == "revised" else 1
        if path == "/documents":
            return 200, {}, {"documents": [{"doc_id": "D1", "revision": revision, "pages": [f"/doc/D1/r{revision}/p1", f"/doc/D1/r{revision}/p2"]}]}
        if path.startswith("/doc/D1/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4:
                rev,page = parts[2],parts[3]
                if self.phase == "partial" and page == "p2":
                    return 503, {}, {"error": "续页读取失败"}
                file = DATA / f"{rev}-{page}.html"
                if file.is_file():
                    return 200, {"Content-Type": "text/html; charset=utf-8"}, file.read_bytes()
        return 404, {}, {"error": "文档不存在"}
