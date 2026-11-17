"""HTML -> Markdown conversion using only the standard library.

Goal: readable, greppable Obsidian notes rather than perfect CommonMark fidelity.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
# <template> holds inert markup, but publishers (Common Dreams, among others) ship
# the article body inside one and render it with script, so its text is real.
SKIP = {"script", "style", "noscript", "svg", "form", "button", "nav"}
BLOCKS = {
    "p", "div", "section", "article", "main", "header", "footer", "aside",
    "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
    "li", "blockquote", "pre", "table", "hr", "dl", "dt", "dd",
    "details", "summary", "video", "audio", "iframe", "template",
}
INLINE_ESCAPES = re.compile(r"([\\`*_\[\]])")
WHITESPACE = re.compile(r"[ \t\r\n\f\v\u00a0]+")


CODE_MARKERS = ("function(", "=>", "window.", "document.", "var ", "return;")


def looks_like_code(text):
    """True for a long stretch of JavaScript/minified code masquerading as prose."""
    sample = text[:20000]
    if len(sample) < 400:
        return False
    if sum(sample.count(c) for c in "{};=") < len(sample) * 0.02:
        return False
    return sum(sample.count(marker) for marker in CODE_MARKERS) >= 3


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<{self.tag} {len(self.children)} children>"


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        # HTML allows unclosed <p>, <li> and table cells; mimic the browser.
        if tag in BLOCKS and self.current.tag == "p":
            self.current = self.current.parent
        if tag in ("li", "tr", "td", "th", "dt", "dd") and self.current.tag == tag:
            self.current = self.current.parent
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        if not data:
            return
        if self.current.tag not in ("pre", "code") and looks_like_code(data):
            # Publishers sometimes ship an inline consent script that the parser
            # escapes into body text; it is code, not prose.
            return
        self.current.children.append(data)


def _text(node):
    """Concatenated literal text of a subtree."""
    if isinstance(node, str):
        return node
    return "".join(_text(child) for child in node.children)


def _attr(node, name):
    value = node.attrs.get(name)
    return value.strip() if isinstance(value, str) else ""


def _escape(text):
    return INLINE_ESCAPES.sub(r"\\\1", text)


def _clean(text):
    return _escape(WHITESPACE.sub(" ", text))


class Renderer:
    def __init__(self):
        self.blocks = []

    # ------------------------------------------------------------------ inline

    def inline(self, node):
        if isinstance(node, str):
            return _clean(node)
        tag = node.tag
        if tag in SKIP:
            return ""
        if tag == "br":
            return "  \n"
        if tag == "img":
            src = _attr(node, "src") or _attr(node, "data-src")
            if not src:
                return ""
            alt = _escape(_attr(node, "alt"))
            return f"![{alt}]({src})"
        if tag in ("strong", "b"):
            return self._wrap(node, "**")
        if tag in ("em", "i", "cite"):
            return self._wrap(node, "*")
        if tag == "del" or tag == "s":
            return self._wrap(node, "~~")
        if tag in ("code", "tt", "kbd", "samp"):
            raw = WHITESPACE.sub(" ", _text(node)).strip()
            if not raw:
                return ""
            fence = "`" * (_longest_run(raw, "`") + 1)
            return f"{fence}{raw}{fence}"
        if tag == "a":
            label = self.inline_children(node).strip()
            href = _attr(node, "href")
            if not href or href.startswith("javascript:"):
                return label
            if not label:
                return f"<{href}>"
            return f"[{label}]({href})"
        if tag in ("sup", "sub"):
            inner = self.inline_children(node).strip()
            return f"^{inner}^" if tag == "sup" and inner else inner
        return self.inline_children(node)

    def inline_children(self, node):
        return "".join(self.inline(child) for child in node.children)

    def _wrap(self, node, marker):
        inner = self.inline_children(node).strip()
        if not inner:
            return ""
        return f"{marker}{inner}{marker}"

    # ------------------------------------------------------------------ blocks

    def blocks_of(self, node, depth=0):
        """Render a node's children into a list of block strings."""
        if isinstance(node, str):
            text = WHITESPACE.sub(" ", _escape(node)).strip()
            return [text] if text else []

        tag = node.tag
        if tag in SKIP:
            return []
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self.inline_children(node).strip()
            return [f"{'#' * int(tag[1])} {text}"] if text else []
        if tag == "p":
            text = self.inline_children(node).strip()
            return [text] if text else []
        if tag == "hr":
            return ["---"]
        if tag == "pre":
            return [self._code_block(node)]
        if tag == "blockquote":
            inner = "\n\n".join(self.blocks_of_children(node, depth))
            if not inner:
                return []
            return ["\n".join("> " + line if line else ">" for line in inner.split("\n"))]
        if tag in ("ul", "ol"):
            return self._list(node, ordered=(tag == "ol"), depth=depth)
        if tag == "table":
            return self._table(node)
        if tag in ("figure", "figcaption"):
            inner = self.blocks_of_children(node, depth)
            if tag == "figcaption" and inner:
                return [f"*{' '.join(inner).strip()}*"]
            return inner
        if tag in ("video", "audio", "iframe"):
            src = _attr(node, "src")
            title = _attr(node, "title") or node.tag
            return [f"[{title}]({src})"] if src else []
        if tag in BLOCKS:
            return self.blocks_of_children(node, depth)
        # Unknown or inline element: treat its children as blocks.
        return self.blocks_of_children(node, depth)

    def blocks_of_children(self, node, depth=0):
        """Split mixed inline/block content into block strings."""
        out, buffer = [], []

        def flush():
            if buffer:
                text = "".join(buffer).strip()
                if text:
                    out.append(text)
                del buffer[:]

        for child in node.children:
            if isinstance(child, str) or not isinstance(child, Node):
                buffer.append(self.inline(child))
            elif child.tag not in BLOCKS:
                buffer.append(self.inline(child))
            else:
                flush()
                out.extend(self.blocks_of(child, depth))
        flush()
        return out

    # ------------------------------------------------------------------- lists

    def _list(self, node, ordered, depth):
        lines = []
        trailing = []
        index = 1
        for child in node.children:
            if not isinstance(child, Node):
                continue
            if child.tag != "li":
                # Browsers keep stray <p>s between <li>s visible; hoist them out
                # of the list instead of dropping their text.
                trailing.extend(self.blocks_of(child, depth))
                continue
            inline_parts = []
            nested = []
            for grandchild in child.children:
                if isinstance(grandchild, Node) and grandchild.tag in ("ul", "ol"):
                    nested.extend(self._list(grandchild, grandchild.tag == "ol", depth + 1))
                elif isinstance(grandchild, Node) and grandchild.tag in BLOCKS:
                    inline_parts.append(" ".join(self.blocks_of(grandchild, depth)))
                else:
                    inline_parts.append(self.inline(grandchild))
            text = re.sub(r"\s+", " ", " ".join(p for p in inline_parts if p)).strip()
            marker = f"{index}. " if ordered else "- "
            index += 1
            indent = "  " * depth
            first = f"{indent}{marker}{text}".rstrip()
            lines.append(first)
            for line in nested:
                lines.append(line)
        return (["\n".join(lines)] if lines else []) + trailing

    # ------------------------------------------------------------------- table

    def _table(self, node):
        rows = []

        def walk(current):
            for child in current.children:
                if isinstance(child, Node):
                    if child.tag == "tr":
                        rows.append(child)
                    elif child.tag in ("thead", "tbody", "tfoot"):
                        walk(child)

        walk(node)
        if not rows:
            return []

        rendered = []
        for row in rows:
            cells = []
            for cell in row.children:
                if isinstance(cell, Node) and cell.tag in ("td", "th"):
                    cells.append(re.sub(r"\s*\n\s*", " ", self.inline_children(cell)).strip())
            if cells:
                rendered.append(cells)
        if not rendered:
            return []

        width = max(len(row) for row in rendered)
        lines = []
        for position, cells in enumerate(rendered):
            cells = cells + [""] * (width - len(cells))
            lines.append("| " + " | ".join(cells) + " |")
            if position == 0:
                lines.append("|" + "|".join([" --- "] * width) + "|")
        return ["\n".join(lines)]

    # -------------------------------------------------------------------- code

    @staticmethod
    def _code_block(node):
        code_node = next(
            (c for c in node.children if isinstance(c, Node) and c.tag == "code"), None
        )
        language = ""
        if code_node is not None:
            classes = (_attr(code_node, "class") or "").split()
            for item in classes:
                if item.startswith("language-"):
                    language = item[len("language-"):]
                    break
        source = _text(code_node if code_node is not None else node)
        source = source.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        fence = "`" * max(3, _longest_run(source, "`") + 1)
        return f"{fence}{language}\n{source}\n{fence}"

    # ---------------------------------------------------------------- document

    def render(self, node):
        blocks = self.blocks_of_children(node)
        text = "\n\n".join(blocks)
        text = re.sub(r"(?m)(?<!\s) \n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _longest_run(text, char):
    longest = 0
    current = 0
    for character in text:
        if character == char:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def html_to_markdown(html):
    """Convert an HTML fragment or document to Markdown."""
    if not html or not html.strip():
        return ""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return Renderer().render(builder.root)
