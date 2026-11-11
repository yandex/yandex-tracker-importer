import re
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Union

from .utils import encode_url

# `src` and `alt` of an image -> its markdown; None keeps the default ``![alt](src)``
ImageRenderer = Callable[[str, str], Optional[str]]

_VOID_TAGS = {"br", "img", "input", "hr", "col", "meta", "link", "source", "wbr"}
_BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "blockquote", "hr", "table",
    "figure", "section", "article", "header", "footer",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# a list item closes an open item, a paragraph closes an open paragraph
_IMPLIED_END = {"li": {"li"}, "p": {"p"}, "tr": {"tr", "td", "th"}, "td": {"td", "th"}, "th": {"td", "th"}}
_ESCAPED = re.compile(r"([\\`*_\[\]])")
_CODE_BLANK_LINE = "\x00"
# text at the start of a line that markdown takes for a heading, a quote, a list item or a table row
_BLOCK_MARKER = re.compile(r"^(?:([#>+|]|-+)|(\d+)([.)]))(?=\s|$)", re.MULTILINE)


def _escape_block_marker(match: re.Match) -> str:
    return f"\\{match.group(1)}" if match.group(1) else f"{match.group(2)}\\{match.group(3)}"


class Node:
    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List[Union["Node", str]] = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        closing = _IMPLIED_END.get(tag, set())
        if closing and self._stack[-1].tag in closing:
            self._stack.pop()
        node = Node(tag, {name: value or "" for name, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(Node(tag, {name: value or "" for name, value in attrs}))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def html_to_markdown(html: Optional[str], render_image: Optional[ImageRenderer] = None) -> str:
    """HTML of a WEEEK description -> markdown: paragraphs, headings, emphasis, links, lists with checkboxes,
    code, quotes, tables and images."""
    if not html:
        return ""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    text = _Renderer(render_image).blocks(builder.root.children)
    return re.sub(r"\n{3,}", "\n\n", text).strip().replace(_CODE_BLANK_LINE, "")


class _Renderer:
    def __init__(self, render_image: Optional[ImageRenderer]):
        self._render_image = render_image

    # -- blocks ---------------------------------------------------------------

    def blocks(self, children: List[Union[Node, str]], separator: str = "\n\n") -> str:
        result: List[str] = []
        inline: List[Union[Node, str]] = []

        def flush():
            text = _BLOCK_MARKER.sub(_escape_block_marker, self._inline_text(inline))
            if text:
                result.append(text)
            inline.clear()

        for child in children:
            if isinstance(child, Node) and child.tag in _BLOCK_TAGS:
                flush()
                block = self.block(child)
                if block:
                    result.append(block)
            else:
                inline.append(child)
        flush()
        return separator.join(result)

    def block(self, node: Node) -> str:
        tag = node.tag
        if tag in _HEADINGS:
            text = self._inline_text(node.children).replace("\n", " ")
            return f"{'#' * _HEADINGS[tag]} {text}" if text else ""
        if tag in ("ul", "ol"):
            return self._list(node)
        if tag == "li":
            return self._list_item(node, "- ")
        if tag == "pre":
            return self._code_block(node)
        if tag == "blockquote":
            inner = self.blocks(node.children)
            return "\n".join(f"> {line}" if line else ">" for line in inner.split("\n")) if inner else ""
        if tag == "hr":
            return "---"
        if tag == "table":
            return self._table(node)
        return self.blocks(node.children)

    def _list(self, node: Node) -> str:
        items = []
        number = int(node.attrs.get("start") or 1) if node.tag == "ol" and (node.attrs.get("start") or "1").isdigit() else 1
        for child in node.children:
            if isinstance(child, str):
                if child.strip():
                    items.append(self._list_item(Node("li"), "- ", [child]))
                continue
            if child.tag in ("ul", "ol"):
                items.append(self._indent(self._list(child), "  "))
                continue
            marker = f"{number}. " if node.tag == "ol" else "- "
            items.append(self._list_item(child, marker))
            number += 1
        return "\n".join(item for item in items if item)

    def _list_item(self, node: Node, marker: str, children: Optional[List[Union[Node, str]]] = None) -> str:
        children = list(node.children if children is None else children)
        checkbox = self._take_checkbox(children)
        if checkbox is not None:
            marker += "[x] " if checkbox else "[ ] "
        content = self.blocks(children, separator="\n")
        lines = content.split("\n") if content else [""]
        indent = " " * len(marker.split("[")[0])
        return "\n".join([marker + lines[0]] + [indent + line if line else "" for line in lines[1:]]).rstrip()

    @staticmethod
    def _take_checkbox(children: List[Union[Node, str]]) -> Optional[bool]:
        """Removes the leading checkbox of a list item; returns whether it is checked, None without it."""
        for index, child in enumerate(children):
            if isinstance(child, str):
                if child.strip():
                    return None
                continue
            if child.tag == "input" and child.attrs.get("type") == "checkbox":
                del children[index]
                return "checked" in child.attrs
            return None
        return None

    @staticmethod
    def _indent(text: str, indent: str) -> str:
        return "\n".join(indent + line if line else "" for line in text.split("\n"))

    def _code_block(self, node: Node) -> str:
        code = self._raw_text(node).strip("\n")
        language = ""
        for child in node.children:
            if isinstance(child, Node) and child.tag == "code":
                classes = child.attrs.get("class", "").split()
                language = next((c[len("language-"):] for c in classes if c.startswith("language-")), "")
        fence = "```"
        while fence in code:
            fence += "`"
        # blank lines of code are marked to survive the collapse of blank lines between blocks
        code = "\n".join(line or _CODE_BLANK_LINE for line in code.split("\n"))
        return f"{fence}{language}\n{code}\n{fence}"

    def _table(self, node: Node) -> str:
        rows = []
        for row in self._rows(node):
            cells = [
                self._inline_text(cell.children).replace("\n", " ").replace("|", "\\|")
                for cell in row.children if isinstance(cell, Node) and cell.tag in ("td", "th")
            ]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
        return "\n".join(lines)

    def _rows(self, node: Node) -> List[Node]:
        rows = []
        for child in node.children:
            if isinstance(child, Node):
                if child.tag == "tr":
                    rows.append(child)
                elif child.tag in ("thead", "tbody", "tfoot"):
                    rows.extend(self._rows(child))
        return rows

    # -- inline ---------------------------------------------------------------

    def _inline_text(self, children: List[Union[Node, str]]) -> str:
        text = "".join(self._inline(child) for child in children)
        # spaces around line breaks are not content
        text = re.sub(r" *\n *", "\n", text)
        return text.strip()

    def _inline(self, node: Union[Node, str]) -> str:
        if isinstance(node, str):
            return _ESCAPED.sub(r"\\\1", re.sub(r"\s+", " ", node))
        tag = node.tag
        if tag == "br":
            return "\n"
        if tag == "img":
            return self._image(node)
        if tag == "input":
            if node.attrs.get("type") == "checkbox":
                return "[x] " if "checked" in node.attrs else "[ ] "
            return ""
        if tag == "code":
            code = self._raw_text(node)
            fence = "``" if "`" in code else "`"
            return f"{fence}{code}{fence}" if code else ""
        if tag in _BLOCK_TAGS:
            return "\n" + self.block(node) + "\n"
        inner = "".join(self._inline(child) for child in node.children)
        if tag in ("strong", "b"):
            return self._wrap(inner, "**")
        if tag in ("em", "i"):
            return self._wrap(inner, "*")
        if tag in ("s", "del", "strike"):
            return self._wrap(inner, "~~")
        if tag == "a":
            href = node.attrs.get("href", "")
            label = inner.strip() or href
            return f"[{label}]({encode_url(href)})" if href else inner
        return inner

    @staticmethod
    def _wrap(inner: str, marker: str) -> str:
        """Emphasis keeps the spaces around its text outside the markers."""
        stripped = inner.strip()
        if not stripped:
            return inner
        leading = inner[:len(inner) - len(inner.lstrip())]
        trailing = inner[len(inner.rstrip()):]
        return f"{leading}{marker}{stripped}{marker}{trailing}"

    def _image(self, node: Node) -> str:
        src, alt = node.attrs.get("src", ""), node.attrs.get("alt", "")
        if self._render_image:
            rendered = self._render_image(src, alt)
            if rendered is not None:
                return rendered
        return f"![{alt}]({encode_url(src)})" if src else ""

    def _raw_text(self, node: Union[Node, str]) -> str:
        if isinstance(node, str):
            return node
        if node.tag == "br":
            return "\n"
        return "".join(self._raw_text(child) for child in node.children)
