from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}


def _q(local: str) -> str:
    return f"{{{W_NS}}}{local}"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def _clean_multiline_text(value: str) -> str:
    """Normalize each Word line without destroying explicit line breaks."""

    lines = [clean_text(line) for line in value.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def _node_text(node: ET.Element) -> str:
    parts: list[str] = []
    deleted_depth = 0
    for event, element in _walk_events(node):
        if event == "start" and element.tag == _q("del"):
            deleted_depth += 1
        elif event == "end" and element.tag == _q("del"):
            deleted_depth = max(0, deleted_depth - 1)
        elif event == "end" and deleted_depth == 0:
            if element.tag == _q("t") and element.text:
                parts.append(element.text)
            elif element.tag == _q("tab"):
                parts.append("\t")
            elif element.tag in {_q("br"), _q("cr")}:
                parts.append("\n")
    return _clean_multiline_text("".join(parts))


def _walk_events(root: ET.Element):
    """Small ElementTree event iterator that does not require lxml."""

    def walk(node: ET.Element):
        yield "start", node
        for child in node:
            yield from walk(child)
        yield "end", node

    return walk(root)


def _attr(node: ET.Element | None, name: str) -> str | None:
    return None if node is None else node.get(_q(name))


@dataclass(frozen=True, slots=True)
class ParsedCell:
    col: int
    span: int
    text: str


@dataclass(frozen=True, slots=True)
class ParsedRow:
    cells: tuple[ParsedCell, ...]
    logical_cols: int

    def as_map(self) -> dict[int, str]:
        return {cell.col: cell.text for cell in self.cells}

    def as_list(self) -> list[str]:
        values = [""] * self.logical_cols
        for cell in self.cells:
            values[cell.col] = cell.text
        return values


@dataclass(frozen=True, slots=True)
class ParsedTable:
    rows: tuple[ParsedRow, ...]
    grid: tuple[int, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(" | ".join(row.as_list()) for row in self.rows)


@dataclass(frozen=True, slots=True)
class BodyItem:
    kind: str
    index: int


@dataclass(slots=True)
class ParsedDocument:
    source_path: Path
    normalized_path: Path
    paragraphs: list[str] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    body_items: list[BodyItem] = field(default_factory=list)
    sha256: str = ""

    @property
    def text(self) -> str:
        ordered: list[str] = []
        for item in self.body_items:
            if item.kind == "paragraph":
                ordered.append(self.paragraphs[item.index])
            elif item.kind == "table":
                ordered.append(self.tables[item.index].text)
        return "\n".join(value for value in ordered if value)


def _parse_table(table_element: ET.Element) -> ParsedTable:
    grid = tuple(
        int(width)
        for column in table_element.findall("./w:tblGrid/w:gridCol", NS)
        if (width := _attr(column, "w")) and width.isdigit()
    )
    rows: list[ParsedRow] = []
    for row_element in table_element.findall("./w:tr", NS):
        logical_col = 0
        cells: list[ParsedCell] = []
        for cell_element in row_element.findall("./w:tc", NS):
            properties = cell_element.find("./w:tcPr", NS)
            span = 1
            if properties is not None:
                raw_span = _attr(properties.find("./w:gridSpan", NS), "val")
                if raw_span and raw_span.isdigit():
                    span = int(raw_span)
            cells.append(ParsedCell(logical_col, span, _node_text(cell_element)))
            logical_col += span
        rows.append(ParsedRow(tuple(cells), logical_col))
    return ParsedTable(tuple(rows), grid)


def read_docx(path: Path, *, source_path: Path | None = None) -> ParsedDocument:
    path = path.resolve()
    raw = path.read_bytes()
    parsed = ParsedDocument(
        source_path=(source_path or path).resolve(),
        normalized_path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    with zipfile.ZipFile(path) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    body = root.find("./w:body", NS)
    if body is None:
        raise ValueError(f"DOCX has no document body: {path}")
    for child in list(body):
        if child.tag == _q("p"):
            index = len(parsed.paragraphs)
            parsed.paragraphs.append(_node_text(child))
            parsed.body_items.append(BodyItem("paragraph", index))
        elif child.tag == _q("tbl"):
            index = len(parsed.tables)
            parsed.tables.append(_parse_table(child))
            parsed.body_items.append(BodyItem("table", index))
    return parsed
