from __future__ import annotations

from collections.abc import Iterable
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile

from docx import Document
from docx.document import Document as DocxDocument

from .document_metadata import GENERATED_DOCUMENT_IDENTIFIER
from .docx_package import sanitize_docx_package


def resolve_docx_output_path(
    output_path: Path,
    *,
    template_path: Path | None = None,
    source_paths: Iterable[Path] = (),
) -> Path:
    """Resolve and validate a DOCX destination against immutable inputs."""

    output = output_path.resolve()
    if output.suffix.casefold() != ".docx":
        raise ValueError("output_path must use the .docx extension")
    if template_path is not None and output == template_path.resolve():
        raise ValueError("output_path must not overwrite the canonical template")
    if output in {path.resolve() for path in source_paths}:
        raise ValueError("output_path must not overwrite an immutable source document")
    return output


def save_sanitized_docx_atomically(
    document: DocxDocument,
    output_path: Path,
) -> Path:
    """Publish a sanitized, readable DOCX without exposing an intermediate output."""

    output = output_path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    document.core_properties.identifier = GENERATED_DOCUMENT_IDENTIFIER
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            prefix=f".{output.stem}-",
            suffix=".docx",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        document.save(temporary_path)
        sanitize_docx_package(temporary_path)
        Document(temporary_path)
        replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output
