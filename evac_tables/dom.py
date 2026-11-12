"""极简 DOM：只为表格抽取保留标签、属性、文本与嵌套结构。

标准库 ``html.parser`` 负责词法，本模块负责建立可回溯的树。解析结果不用于
渲染，因此不处理浏览器纠错规则；遇到无法配对的结束标签时保留已读内容并继续。
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# 这些元素的内容不属于单元格的业务文字。
NON_TEXT_TAGS = {"script", "style", "template"}


class Node:
    """元素节点或文本节点（``tag is None``）。"""

    __slots__ = ("tag", "attrs", "children", "parent", "text", "span")

    def __init__(self, tag=None, attrs=None, text=None, parent=None, span=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent
        self.text = text
        self.span = span

    def get(self, name, default=None):
        return self.attrs.get(name, default)

    @property
    def is_element(self):
        return self.tag is not None

    @property
    def is_text(self):
        return self.tag is None

    def append(self, child):
        child.parent = self
        self.children.append(child)
        return child

    def elements(self, tag=None):
        for child in self.children:
            if child.tag is not None and (tag is None or child.tag == tag):
                yield child

    def descendants(self, tag=None, stop_at=None):
        """深度优先遍历元素；``stop_at`` 中的标签不再向下展开。"""
        for child in self.children:
            if child.tag is None:
                continue
            if tag is None or child.tag == tag:
                yield child
            if stop_at and child.tag in stop_at:
                continue
            yield from child.descendants(tag, stop_at)


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#document")
        self.stack = [self.root]

    @property
    def current(self):
        return self.stack[-1]

    def _element(self, tag, attrs):
        node = Node(tag=tag.lower(), attrs={k.lower(): v for k, v in attrs})
        self.current.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._element(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._element(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # 没有对应的开始标签：忽略，而不是把后续内容挂错位置。

    def handle_data(self, data):
        if not data:
            return
        last = self.current.children[-1] if self.current.children else None
        if last is not None and last.is_text:
            last.text += data
            return
        self.current.append(Node(text=data))


def parse_html(text):
    """解析 HTML 片段，返回 ``#document`` 根节点。"""
    builder = _TreeBuilder()
    builder.feed(text or "")
    builder.close()
    return builder.root


def find(node, predicate, stop_at=None):
    """返回第一个满足条件的元素。"""
    for child in node.descendants(stop_at=stop_at):
        if predicate(child):
            return child
    return None


def find_all(node, predicate, stop_at=None):
    return [child for child in node.descendants(stop_at=stop_at) if predicate(child)]


def by_tag(node, tag, stop_at=None):
    return find_all(node, lambda n: n.tag == tag, stop_at=stop_at)


def attr_text(value):
    return "" if value is None else str(value).strip()


_WS = re.compile(r"\s+")


def normalize_text(text):
    """压缩空白并去掉首尾空白；保留单元格内部文字。"""
    return _WS.sub(" ", text or "").strip()


def text_content(node, skip_tags=("table", "sup"), skip_non_text=True):
    """提取元素文字，默认跳过嵌套表格与脚注标记。

    嵌套表格属于备注内容，必须由调用方单独采集，不能混入主体字段。
    """
    parts = []

    def walk(parent):
        for child in parent.children:
            if child.tag is None:
                parts.append(child.text or "")
                continue
            if skip_non_text and child.tag in NON_TEXT_TAGS:
                continue
            if child.tag in skip_tags:
                continue
            if child.tag == "br":
                parts.append(" ")
                continue
            walk(child)

    walk(node)
    return normalize_text("".join(parts))


def outer_html(node):
    """序列化元素（用于证据中的原始单元格片段）。"""
    if node.tag is None:
        return escape(node.text or "", quote=False)
    attrs = "".join(
        ' {}="{}"'.format(key, escape(str(value or ""), quote=True))
        for key, value in node.attrs.items()
    )
    tag = node.tag
    if tag in VOID_TAGS:
        return f"<{tag}{attrs}>"
    inner = "".join(outer_html(child) for child in node.children)
    return f"<{tag}{attrs}>{inner}</{tag}>"


def nearest_ancestor(node, tag):
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return parent
        parent = parent.parent
    return None
