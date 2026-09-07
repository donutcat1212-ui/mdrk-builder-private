from __future__ import annotations

import zipfile
import subprocess
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
    session=None,
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
        for index, source_path in enumerate(source_files):
            if session:
                session.check()
                session.progress(index, len(source_files), source_path)
            try:
                fingerprint, cached = session.cached(source_path) if session else (None, None)
                if cached is not None:
                    documents.append(cached)
                    continue
                normalized_path = normalizer.normalize(source_path)
                document = read_docx(normalized_path, source_path=source_path)
                scanned = ScannedDocument(document=document, classification=classify_document(document))
                documents.append(scanned)
                if session:
                    session.put(source_path, fingerprint, scanned)
            except (
                ConversionError,
                subprocess.SubprocessError,
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
    if session:
        session.progress(len(source_files), len(source_files), None)
        session.check()
    return SourceScanResult(
        source_files=tuple(source_files),
        documents=tuple(documents),
        failures=tuple(failures),
        root=folder,
    )
