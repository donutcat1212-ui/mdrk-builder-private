from __future__ import annotations

from zipfile import ZipFile

from docx import Document

from mdrk_builder.infrastructure.docx_layout import (
    BOTTOM_MARGIN_DXA,
    LEFT_MARGIN_DXA,
    PAGE_HEIGHT_DXA,
    PAGE_WIDTH_DXA,
    RIGHT_MARGIN_DXA,
    TOP_MARGIN_DXA,
)
from mdrk_builder.infrastructure.docx_template import (
    FONT_NAME,
    FONT_SIZE_PT,
    STYLE_BODY,
    STYLE_LABEL,
    STYLE_MCF_CODE,
    STYLE_MEETING,
    STYLE_SECTION,
    STYLE_TABLE,
    STYLE_TABLE_HEADER,
    STYLE_TASK,
    STYLE_TITLE,
    STYLE_WARNING,
    TABLE_FONT_SIZE_PT,
    canonical_template_path,
    create_canonical_template,
)


def test_canonical_resource_has_sanitized_page_and_style_contract() -> None:
    path = canonical_template_path()
    assert path.is_file()

    document = Document(path)
    assert len(document.sections) == 1
    section = document.sections[0]
    assert section.page_width.twips == PAGE_WIDTH_DXA
    assert section.page_height.twips == PAGE_HEIGHT_DXA
    assert section.left_margin.twips == LEFT_MARGIN_DXA
    assert section.right_margin.twips == RIGHT_MARGIN_DXA
    assert section.top_margin.twips == TOP_MARGIN_DXA
    assert section.bottom_margin.twips == BOTTOM_MARGIN_DXA

    expected_styles = (
        STYLE_BODY,
        STYLE_TITLE,
        STYLE_MEETING,
        STYLE_SECTION,
        STYLE_LABEL,
        STYLE_TABLE,
        STYLE_TABLE_HEADER,
        STYLE_MCF_CODE,
        STYLE_TASK,
        STYLE_WARNING,
    )
    table_styles = {STYLE_TABLE, STYLE_TABLE_HEADER, STYLE_MCF_CODE}
    for name in expected_styles:
        style = document.styles[name]
        assert style.font.name == FONT_NAME
        expected_size = TABLE_FONT_SIZE_PT if name in table_styles else FONT_SIZE_PT
        assert style.font.size.pt == expected_size

    properties = document.core_properties
    assert properties.author == "MDRK Builder"
    assert properties.last_modified_by == "MDRK Builder"
    assert properties.comments == "Санитизированный служебный ресурс без данных пациента"

    with ZipFile(path) as archive:
        names = archive.namelist()
        assert not any(name.startswith("customXml/") for name in names)
        assert "docProps/thumbnail.jpeg" not in names
        package_text = "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in names
            if name.endswith((".xml", ".rels"))
        )
    assert "customXml" not in package_text
    assert "thumbnail.jpeg" not in package_text
    assert "ФИО пациента:" not in package_text
    assert "/Users/" not in package_text


def test_canonical_rebuild_is_structurally_stable(tmp_path) -> None:
    first = create_canonical_template(tmp_path / "first.docx")
    second = create_canonical_template(tmp_path / "second.docx")
    stable_parts = (
        "word/document.xml",
        "word/styles.xml",
        "word/settings.xml",
        "word/numbering.xml",
        "word/theme/theme1.xml",
    )
    with ZipFile(first) as first_archive, ZipFile(second) as second_archive:
        for part in stable_parts:
            assert first_archive.read(part) == second_archive.read(part)
