"""轻量 HTML 解析（标准库实现，零第三方依赖）。

只支持本项目选择器所需的子集：
- 标签名 div、h1；类 .foo；标签+类 div.foo；
- 后代组合（空格），如 "section.bid-detail h2.bid-title"；
- select_one 返回第一个命中节点的纯文本。
"""
from __future__ import annotations

from html.parser import HTMLParser

_VOID_TAGS = {"br", "img", "input", "meta", "link", "hr", "area", "base",
              "col", "embed", "source", "track", "wbr"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text_parts")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "_Node | None"):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.parent = parent
        self.text_parts: list[str] = []

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def get_text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            parts.append(child.get_text())
        return "".join(parts).strip()


class _DOMBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {}, None)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        # 容错：向上找最近的同名标签
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].text_parts.append(data)


def _parse_selector(selector: str) -> list[tuple[str | None, set[str]]]:
    steps = []
    for token in selector.split():
        tag = None
        classes: set[str] = set()
        if "." in token:
            head, rest = token.split(".", 1)
            if head:
                tag = head
            classes.update(c for c in rest.split(".") if c)
        else:
            tag = token
        steps.append((tag, classes))
    return steps


def _match(node: _Node, tag: str | None, classes: set[str]) -> bool:
    if tag is not None and node.tag != tag:
        return False
    if classes and not classes.issubset(node.classes()):
        return False
    return True


def _walk(node: _Node):
    for child in node.children:
        yield child
        yield from _walk(child)


def select_one(html: str, selector: str) -> _Node | None:
    builder = _DOMBuilder()
    builder.feed(html or "")
    steps = _parse_selector(selector)
    if not steps:
        return None

    # 先找命中最后一步的节点，再验证祖先链
    final_tag, final_classes = steps[-1]
    for node in _walk(builder.root):
        if not _match(node, final_tag, final_classes):
            continue
        ancestor = node.parent
        ok = True
        for tag, classes in reversed(steps[:-1]):
            found = False
            cur = ancestor
            while cur is not None and cur is not builder.root:
                if _match(cur, tag, classes):
                    found = True
                    ancestor = cur.parent
                    break
                cur = cur.parent
            if not found:
                ok = False
                break
        if ok:
            return node
    return None


def text_of(html: str, selector: str) -> str:
    node = select_one(html, selector)
    return node.get_text() if node is not None else ""
