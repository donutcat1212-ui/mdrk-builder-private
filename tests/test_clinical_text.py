from datetime import datetime
from pathlib import Path

from mdrk_builder.application.clinical_text import (
    DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ClinicalTextObservation,
    compose_clinical_timeline,
    extract_novel_clinical_text,
    is_empty_clinical_update,
)


def _observation(
    text: str,
    when: datetime,
    *,
    document_type: str = "follow_up",
) -> ClinicalTextObservation:
    return ClinicalTextObservation(
        text=text,
        occurred_at=when,
        document_type=document_type,
        source=Path(f"fixtures/{when:%Y%m%d-%H%M}.docx"),
    )


def test_empty_diary_markers_are_not_clinical_content() -> None:
    assert is_empty_clinical_update("Без изменений.")
    assert is_empty_clinical_update("без дополнений")
    assert is_empty_clinical_update("по ИПМР")
    assert is_empty_clinical_update("без изменений, без дополнений")
    assert is_empty_clinical_update("(дополнения к анамнезу): без дополнений")
    assert not is_empty_clinical_update("Без изменений. Добавлена консультация.")


def test_timeline_keeps_primary_baseline_and_only_dated_new_content() -> None:
    initial = _observation(
        "БАЗОВЫЙ_ФАКТ_А. БАЗОВЫЙ_ФАКТ_Б.",
        datetime(2026, 8, 3, 8, 20),
        document_type="initial",
    )
    unchanged = _observation("без изменений", datetime(2026, 8, 5, 9))
    copied_with_addition = _observation(
        "БАЗОВЫЙ_ФАКТ_А. БАЗОВЫЙ_ФАКТ_Б. "
        "НОВЫЙ_ФАКТ_В.",
        datetime(2026, 8, 7, 10, 15),
    )
    repeated = _observation(
        "НОВЫЙ_ФАКТ_В.",
        datetime(2026, 8, 8, 10, 15),
    )

    initial_result = compose_clinical_timeline(
        [initial, unchanged, copied_with_addition, repeated],
        include_updates=False,
    )
    final_result = compose_clinical_timeline(
        [initial, unchanged, copied_with_addition, repeated],
        include_updates=True,
    )

    assert initial_result.text == "БАЗОВЫЙ_ФАКТ_А. БАЗОВЫЙ_ФАКТ_Б."
    assert final_result.text == (
        "БАЗОВЫЙ_ФАКТ_А. БАЗОВЫЙ_ФАКТ_Б.\n"
        "(Дополнение от 07.08.2026 10:15): НОВЫЙ_ФАКТ_В."
    )
    assert final_result.source == copied_with_addition.source


def test_diagnostic_near_copy_keeps_only_new_sentence() -> None:
    known = (
        "ПОКАЗАТЕЛЬ_А: 120 ЕД, ПОКАЗАТЕЛЬ_Б: 6 ЕД. "
        "КОНТРОЛЬНЫЙ_ТЕКСТ_БЕЗ_ИЗМЕНЕНИЙ."
    )
    candidate = (
        "ПОКАЗАТЕЛЬ_А - 120 ЕД, ПОКАЗАТЕЛЬ_Б: 6 ЕД. "
        "КОНТРОЛЬНЫЙ_ТЕКСТ_БЕЗ_ИЗМЕНЕНИЙ. "
        "ПОКАЗАТЕЛЬ_В: 12 ЕД."
    )

    assert extract_novel_clinical_text(
        candidate,
        known,
        duplicate_threshold=DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ) == "ПОКАЗАТЕЛЬ_В: 12 ЕД."


def test_diagnostic_near_copy_preserves_changed_numeric_result() -> None:
    assert extract_novel_clinical_text(
        "ПОКАЗАТЕЛЬ_А: 110 ЕД, ПОКАЗАТЕЛЬ_Б: 6 ЕД.",
        "ПОКАЗАТЕЛЬ_А: 120 ЕД, ПОКАЗАТЕЛЬ_Б: 6 ЕД.",
        duplicate_threshold=DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ) == "ПОКАЗАТЕЛЬ_А: 110 ЕД, ПОКАЗАТЕЛЬ_Б: 6 ЕД."


def test_run_on_diagnostic_copy_keeps_only_inserted_numeric_tail() -> None:
    assert extract_novel_clinical_text(
        "МАРКЕР А 120 МАРКЕР Б 6 МАРКЕР В 12",
        "МАРКЕР А: 120, МАРКЕР Б 6",
        duplicate_threshold=DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ) == "МАРКЕР В 12"
