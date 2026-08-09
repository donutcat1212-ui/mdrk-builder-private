from docx import Document

from mdrk_builder.ui.app import _generate_smoke_document


def test_packaged_smoke_path_generates_docx(tmp_path) -> None:
    output = _generate_smoke_document(tmp_path)

    assert output.is_file()
    assert Document(output).paragraphs
