"""Convert model Markdown into the small, editable note schema without rendering HTML."""
import json
import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.texmath import texmath_plugin

MAX_DOCUMENT_BYTES = 250_000
MAX_TOKENS = 8_000
MAX_NESTING = 16
MAX_MATH_LENGTH = 8_192
BRACKET_MATH = re.compile(r"\\\[((?:(?!\\\[|\\\])[\s\S])+?)\\\]")
LANGUAGE = re.compile(r"[A-Za-z0-9_+.-]{1,32}\Z")


def _bracket_math(state, silent):
    match = BRACKET_MATH.match(state.src, state.pos)
    if not match:
        return False
    if not silent:
        token = state.push("math_inline_double", "math", 0)
        token.content = match[1]
        token.markup = r"\["
    state.pos = match.end()
    return True


def _guard_dollar_block(rule):
    def guarded(state, start_line, end_line, silent):
        start = state.bMarks[start_line] + state.tShift[start_line]
        if state.parentType == "blockquote" or not state.src.startswith("$$", start):
            return False
        closing = state.src.find("$$", start + 2, state.eMarks[end_line - 1])
        if closing < 0 or state.src[closing - 1] == "\\":
            return False
        line_end = state.src.find("\n", closing + 2)
        if state.src[closing + 2:line_end if line_end >= 0 else len(state.src)].strip():
            # The plugin otherwise scans to a later line ending in $$, swallowing
            # the first formula's suffix and potentially the following formula.
            return False
        return rule(state, start_line, end_line, silent)
    return guarded


def _parser():
    parser = MarkdownIt("commonmark", {"html": False, "linkify": False, "maxNesting": MAX_NESTING})
    parser.enable("strikethrough").use(texmath_plugin, delimiters="brackets")
    # texmath's block rules consume the closing line, including text after \].
    # An inline token instead lets the converter keep that text beside the block.
    parser.block.ruler.disable(["math_block", "math_block_eqno"])
    parser.inline.ruler.before("escape", "bracket_display_math", _bracket_math)
    parser.use(dollarmath_plugin, allow_labels=False, allow_space=False,
               allow_digits=False, double_inline=True)
    # Both plugins register "math_block". Disable both before adding one guarded
    # dollar rule; Ruler.at/disable by name would only reach the first occurrence.
    names = parser.block.ruler.get_active_rules()
    dollar_block = dict(zip(names, parser.block.ruler.getRules()))["math_block"]
    parser.block.ruler.enableOnly(name for name in names if name != "math_block")
    parser.block.ruler.before("fence", "bounded_dollar_block", _guard_dollar_block(dollar_block))
    return parser


PARSER = _parser()


def _safe_link(href):
    if not href or any(ord(char) < 33 or ord(char) == 127 for char in href):
        return False
    try:
        url = urlsplit(href)
        return url.scheme in {"http", "https"} and bool(url.hostname)
    except ValueError:
        return False


def _math(kind, token):
    latex = token.content
    if len(latex.encode("utf-8")) > MAX_MATH_LENGTH:
        opening = token.markup
        closing = {r"\(": r"\)", r"\[": r"\]"}.get(opening, opening)
        return _text(opening + latex + closing, [{"type": "code"}])
    return {"type": kind, "attrs": {"latex": latex.strip()}}


def _text(text, marks):
    node = {"type": "text", "text": text}
    # Tiptap's code mark excludes all other marks, and a mark type is a set member.
    active = {}
    for mark in marks:
        if not mark:
            continue
        if mark["type"] == "code":
            active = {"code": {"type": "code"}}
            break
        active.setdefault(mark["type"], mark.copy())
    if active:
        node["marks"] = list(active.values())
    return node


def _inline(tokens):
    result, marks = [], []
    mark_names = {"strong": "bold", "em": "italic", "s": "strike"}
    for token in tokens:
        kind = token.type
        if token.nesting == 1:
            if kind == "link_open":
                href = token.attrGet("href")
                marks.append({"type": "link", "attrs": {"href": href}} if _safe_link(href) else None)
            else:
                marks.append({"type": mark_names[kind.removesuffix("_open")]})
        elif token.nesting == -1:
            marks.pop()
        elif kind in {"text", "code_inline", "image"}:
            # Image alt text remains readable; its URL never becomes a node or request.
            text = token.content
            if text:
                result.append(_text(text, marks + ([{"type": "code"}] if kind == "code_inline" else [])))
        elif kind in {"softbreak", "hardbreak"}:
            result.append({"type": "hardBreak"})
        elif kind in {"math_inline", "math_inline_double"}:
            result.append(_math("inlineMath" if kind == "math_inline" else "blockMath", token))
        else:
            raise ValueError("unsupported inline token")
    return result


def _inline_blocks(template, tokens):
    """Display math must be a block sibling, never a child of a paragraph."""
    result, content = [], []
    for node in _inline(tokens):
        if node["type"] == "blockMath":
            if content:
                result.append({**template, "content": content})
                content = []
            result.append(node)
        else:
            content.append(node)
    if content or not result:
        result.append({**template, **({"content": content} if content else {})})
    return result


def _blocks(tokens, index=0, closing=None):
    result = []
    containers = {"bullet_list": "bulletList", "ordered_list": "orderedList",
                  "list_item": "listItem", "blockquote": "blockquote"}
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == closing:
            return result, index + 1
        if kind in {"paragraph_open", "heading_open"}:
            template = {"type": "paragraph"} if kind == "paragraph_open" else {
                "type": "heading", "attrs": {"level": int(token.tag[1:])}}
            result.extend(_inline_blocks(template, tokens[index + 1].children or []))
            index += 3
            continue
        if token.nesting == 1:
            name = kind.removesuffix("_open")
            content, index = _blocks(tokens, index + 1, name + "_close")
            node = {"type": containers[name], "content": content}
            if name == "ordered_list":
                node["attrs"] = {"start": token.attrGet("start") or 1}
            if name == "list_item" and (not content or content[0]["type"] != "paragraph"):
                content.insert(0, {"type": "paragraph"})
            if name == "blockquote" and not content:
                content.append({"type": "paragraph"})
            result.append(node)
            continue
        if kind in {"fence", "code_block"}:
            node = {"type": "codeBlock"}
            language = token.info.split()[0] if token.info.split() else ""
            if LANGUAGE.fullmatch(language):
                node["attrs"] = {"language": language}
            if token.content:
                node["content"] = [_text(token.content, [])]
            result.append(node)
        elif kind == "hr":
            result.append({"type": "horizontalRule"})
        elif kind == "math_block":
            node = _math("blockMath", token)
            result.append({"type": "paragraph", "content": [node]} if node["type"] == "text" else node)
        else:
            raise ValueError("unsupported block token")
        index += 1
    if closing:
        raise ValueError("unclosed block")
    return result, index


def answer_document(text: str) -> dict | None:
    """Return a bounded document, or None so callers can preserve the original text."""
    try:
        if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            return None
        # Bound delimiter lookahead before MarkdownIt allocates its token stream.
        if sum(text.count(char) for char in "[]\\") > MAX_TOKENS:
            return None
        tokens = PARSER.parse(text)
        pending, count = list(tokens), 0
        while pending:
            token = pending.pop()
            count += 1
            if count > MAX_TOKENS or token.level >= MAX_NESTING - 1:
                return None
            pending.extend(token.children or [])
        content, _ = _blocks(tokens)
        document = {"type": "doc", "content": content or [{"type": "paragraph"}]}
        if len(json.dumps(document, ensure_ascii=False).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            return None
        return document
    except (ValueError, KeyError, IndexError, RecursionError, UnicodeError):
        # Unsupported or excessive input is not truncated and is not stored partially.
        return None


def plain_text(document: dict) -> str:
    kind = document.get("type")
    if kind == "text":
        return document.get("text", "")
    if kind in {"inlineMath", "blockMath"}:
        return document.get("attrs", {}).get("latex", "")
    if kind == "hardBreak":
        return "\n"
    separator = "\n" if kind in {"doc", "bulletList", "orderedList", "listItem", "blockquote"} else ""
    return separator.join(plain_text(child) for child in document.get("content", []))
