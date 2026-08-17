from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from mdrk_builder.infrastructure.classifier import (
    DocumentClassification,
    classify_document,
)
from mdrk_builder.infrastructure.converter import ConversionError, DocumentNormalizer
from mdrk_builder.infrastructure.ooxml_reader import ParsedDocument, read_docx


@dataclass(frozen=True, slots=True)
class ScannedDocument:
    document: ParsedDocument
    classification: DocumentClassification


@dataclass(frozen=True, slots=True)
class SourceReadFailure:
    source_path: Path
    error: Exception


@dataclass(frozen=True, slots=True)
class SourceScanResult:
    source_files: tuple[Path, ...]
    documents: tuple[ScannedDocument, ...]
    failures: tuple[SourceReadFailure, ...]
    root: Path | None = None


def discover_source_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise NotADirectoryError(folder)
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in DocumentNormalizer.SUPPORTED
            and not path.name.startswith("~$")
        ),
        key=lambda path: str(path).casefold(),
    )


def scan_source_documents(
    folder: Path,
    *,
    normalizer: DocumentNormalizer | None = None,
) -> SourceScanResult:
    folder = folder.resolve()
    source_files = discover_source_files(folder)
    if not source_files:
        return SourceScanResult(
            source_files=(),
            documents=(),
            failures=(),
            root=folder,
        )
    owns_normalizer = normalizer is None
    normalizer = normalizer or DocumentNormalizer()
    documents: list[ScannedDocument] = []
    failures: list[SourceReadFailure] = []
    try:
        for source_path in source_files:
            try:
                normalized_path = normalizer.normalize(source_path)
                document = read_docx(normalized_path, source_path=source_path)
                documents.append(
                    ScannedDocument(
                        document=document,
                        classification=classify_document(document),
                    )
                )
            except (
                ConversionError,
                OSError,
                ValueError,
                KeyError,
                zipfile.BadZipFile,
                ET.ParseError,
            ) as exc:
                failures.append(
                    SourceReadFailure(source_path=source_path, error=exc)
                )
    finally:
        if owns_normalizer:
            normalizer.close()
    return SourceScanResult(
        source_files=tuple(source_files),
        documents=tuple(documents),
        failures=tuple(failures),
        root=folder,
    )
