"""Byte-accurate XML source spans shared by KML editor features."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from xml.parsers import expat


CancellationCheck = Callable[[], bool] | None


class KmlSourceScanCancelled(RuntimeError):
    """Raised when a cooperative source scan is cancelled."""


@dataclass(slots=True)
class XmlSourceNode:
    namespace: str
    local_name: str
    start: int
    content_start: int
    parent: "XmlSourceNode | None" = field(default=None, repr=False)
    self_closing: bool = False
    close_start: int | None = None
    end: int | None = None
    children: list["XmlSourceNode"] = field(default_factory=list, repr=False)


def expanded_name(name: str) -> tuple[str, str]:
    if "}" in name:
        namespace, local_name = name.split("}", 1)
        return namespace, local_name
    return "", name


def tag_end(data: bytes, start: int) -> int:
    quote: int | None = None
    for index in range(start, len(data)):
        value = data[index]
        if quote is not None:
            if value == quote:
                quote = None
        elif value in (ord("'"), ord('"')):
            quote = value
        elif value == ord(">"):
            return index + 1
    raise ValueError("Unterminated XML tag")


def line_indent(data: bytes, offset: int) -> bytes:
    line_start = data.rfind(b"\n", 0, offset) + 1
    prefix = data[line_start:offset]
    return prefix if not prefix.strip(b" \t") else b""


def indent_from_gap(gap: bytes, fallback: bytes) -> bytes:
    newline = gap.rfind(b"\n")
    if newline >= 0:
        indent = gap[newline + 1 :]
        if not indent.strip(b" \t"):
            return indent
    return fallback


def scan_xml_source(
    contents: str,
    *,
    cancellation_check: CancellationCheck = None,
) -> tuple[XmlSourceNode | None, tuple[XmlSourceNode, ...]]:
    """Return an Expat-derived source tree whose offsets address UTF-8 bytes."""
    data = contents.encode("utf-8")
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[XmlSourceNode] = []
    completed: list[XmlSourceNode] = []
    root: XmlSourceNode | None = None

    def cancelled() -> None:
        if cancellation_check is not None and cancellation_check():
            raise KmlSourceScanCancelled("KML source scan was cancelled.")

    def start_element(name: str, _attributes) -> None:
        nonlocal root
        cancelled()
        namespace, local_name = expanded_name(name)
        start = parser.CurrentByteIndex
        node = XmlSourceNode(
            namespace,
            local_name,
            start,
            tag_end(data, start),
            parent=stack[-1] if stack else None,
        )
        node.self_closing = data[start:node.content_start].rstrip().endswith(b"/>")
        if root is None:
            root = node
        stack.append(node)

    def end_element(_name: str) -> None:
        cancelled()
        node = stack.pop()
        if node.self_closing:
            node.close_start = node.content_start
            node.end = node.content_start
        else:
            node.close_start = parser.CurrentByteIndex
            node.end = tag_end(data, node.close_start)
        if node.parent is not None:
            node.parent.children.append(node)
        completed.append(node)

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    chunk_size = 256 * 1024
    for offset in range(0, len(data), chunk_size):
        cancelled()
        parser.Parse(data[offset : offset + chunk_size], False)
    parser.Parse(b"", True)
    cancelled()
    return root, tuple(completed)


def has_ancestor(
    node: XmlSourceNode,
    *,
    namespace: str,
    local_name: str,
) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.namespace == namespace and parent.local_name == local_name:
            return True
        parent = parent.parent
    return False


__all__ = [
    "KmlSourceScanCancelled",
    "XmlSourceNode",
    "expanded_name",
    "has_ancestor",
    "indent_from_gap",
    "line_indent",
    "scan_xml_source",
    "tag_end",
]
