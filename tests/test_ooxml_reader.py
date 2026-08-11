from xml.etree import ElementTree as ET

from mdrk_builder.infrastructure.ooxml_reader import W_NS, _node_text


def test_word_line_breaks_are_preserved_for_section_parsing() -> None:
    paragraph = ET.fromstring(
        f'<w:p xmlns:w="{W_NS}">'
        "<w:r><w:t>Анамнез заболевания: МАРКЕР_А</w:t><w:br/>"
        "<w:t>Анамнез жизни: МАРКЕР_Б</w:t></w:r></w:p>"
    )

    assert _node_text(paragraph) == (
        "Анамнез заболевания: МАРКЕР_А\nАнамнез жизни: МАРКЕР_Б"
    )
