from xml.etree import ElementTree as ET

from docx import Document

from mdrk_builder.infrastructure.document_metadata import (
    GENERATED_DOCUMENT_IDENTIFIER,
)
from mdrk_builder.infrastructure.ooxml_reader import W_NS, _node_text, read_docx


def test_word_line_breaks_are_preserved_for_section_parsing() -> None:
    paragraph = ET.fromstring(
        f'<w:p xmlns:w="{W_NS}">'
        "<w:r><w:t>Анамнез заболевания: МАРКЕР_А</w:t><w:br/>"
        "<w:t>Анамнез жизни: МАРКЕР_Б</w:t></w:r></w:p>"
    )

    assert _node_text(paragraph) == (
        "Анамнез заболевания: МАРКЕР_А\nАнамнез жизни: МАРКЕР_Б"
    )


def test_generated_document_identifier_is_read_from_core_properties(tmp_path) -> None:
    path = tmp_path / "generated.docx"
    document = Document()
    document.add_paragraph("Тест")
    document.core_properties.identifier = GENERATED_DOCUMENT_IDENTIFIER
    document.save(path)

    parsed = read_docx(path)

    assert parsed.core_identifier == GENERATED_DOCUMENT_IDENTIFIER
    assert parsed.is_generated_output
