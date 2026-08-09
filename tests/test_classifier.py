from pathlib import Path

from mdrk_builder.domain import SpecialistRole
from mdrk_builder.infrastructure.classifier import classify_document
from mdrk_builder.infrastructure.ooxml_reader import BodyItem, ParsedDocument


def _document(path: str, text: str) -> ParsedDocument:
    return ParsedDocument(
        source_path=Path(path),
        normalized_path=Path(path),
        paragraphs=[text],
        body_items=[BodyItem("paragraph", 0)],
    )


def test_administrative_reverse_sheet_does_not_become_neuropsychology() -> None:
    classification = classify_document(
        _document(
            "/patient/оборотная сторона раздела.docx",
            "Консультация медицинского психолога (нейропсихолога)",
        )
    )

    assert classification.role is SpecialistRole.OTHER
    assert classification.document_type == "administrative"


def test_gastrostomy_consilium_is_excluded_from_physician_sources() -> None:
    classification = classify_document(
        _document(
            "/patient/невролог/консилиум гастростома.docx",
            "Консилиум по вопросу установки гастростомы (ПЭГ)",
        )
    )

    assert classification.role is SpecialistRole.OTHER
    assert classification.document_type == "other_consilium"
    assert not classification.is_mdrk


def test_mdrk_is_recognized_before_specialist_mentions() -> None:
    classification = classify_document(
        _document(
            "/patient/Консилиум 2.docx",
            "Консилиум мультидисциплинарной реабилитационной команды: невролог, логопед, ФТ",
        )
    )

    assert classification.is_mdrk
    assert classification.document_type == "mdrk"


def test_explicit_frm_job_title_overrides_neurology_folder_hint() -> None:
    classification = classify_document(
        _document(
            "/patient/невролог/первичный осмотр.docx",
            "Первичный осмотр. Лечащий врач, врач физической и реабилитационной медицины.",
        )
    )

    assert classification.role is SpecialistRole.FRM


def test_incidental_frm_mention_does_not_override_profile_specialist() -> None:
    classification = classify_document(
        _document(
            "/patient/лого/первичная консультация логопеда.docx",
            "Первичная консультация логопеда. Согласовано: врач ФРМ.",
        )
    )

    assert classification.role is SpecialistRole.LOGOPEDIST
