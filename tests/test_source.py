"""来源样本和本地HTTP服务的回归检查。"""
import hashlib
import json
import unittest
from urllib.request import urlopen
from source_site.source import Source
from source_site.__main__ import running


class SourceTests(unittest.TestCase):
    def test_source_1(self):
        s = Source()
        self.assertEqual(2, len(s.request("GET", "/documents")[2]["documents"][0]["pages"]))

    def test_source_2(self):
        s = Source()
        self.assertIn(b"rowspan", s.request("GET", "/doc/D1/r1/p1")[2])

    def test_source_3(self):
        s = Source()
        s.phase = "revised"
        self.assertEqual(2, s.request("GET", "/documents")[2]["documents"][0]["revision"])

    def test_source_4(self):
        s = Source()
        s.phase = "partial"
        self.assertEqual(503, s.request("GET", "/doc/D1/r1/p2")[0])

    def test_http_source(self):
        with running() as (base, source):
            with urlopen(base + "/documents", timeout=3) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read())


if __name__ == "__main__":
    unittest.main()
