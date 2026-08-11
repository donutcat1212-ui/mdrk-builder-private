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
        source=Path(f"/patient/{when:%Y%m%d-%H%M}.docx"),
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
        "Острое начало. Получает терапию.",
        datetime(2026, 8, 3, 8, 20),
        document_type="initial",
    )
    unchanged = _observation("без изменений", datetime(2026, 8, 5, 9))
    copied_with_addition = _observation(
        "Острое начало. Получает терапию. "
        "Появилась положительная динамика.",
        datetime(2026, 8, 7, 10, 15),
    )
    repeated = _observation(
        "Появилась положительная динамика.",
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

    assert initial_result.text == "Острое начало. Получает терапию."
    assert final_result.text == (
        "Острое начало. Получает терапию.\n"
        "(Дополнение от 07.08.2026 10:15): Появилась положительная динамика."
    )
    assert final_result.source == copied_with_addition.source


def test_diagnostic_near_copy_keeps_only_new_sentence() -> None:
    known = (
        "Клинический анализ крови: Hb 120 г/л, лейкоциты 6,0. "
        "Общий анализ мочи без патологии."
    )
    candidate = (
        "Клинический анализ крови - Hb 120 г/л, лейкоциты 6,0. "
        "Общий анализ мочи без патологии. "
        "С-реактивный белок 12 мг/л."
    )

    assert extract_novel_clinical_text(
        candidate,
        known,
        duplicate_threshold=DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ) == "С-реактивный белок 12 мг/л."


def test_diagnostic_near_copy_preserves_changed_numeric_result() -> None:
    assert extract_novel_clinical_text(
        "Клинический анализ крови: Hb 110 г/л, лейкоциты 6,0.",
        "Клинический анализ крови: Hb 120 г/л, лейкоциты 6,0.",
        duplicate_threshold=DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ) == "Клинический анализ крови: Hb 110 г/л, лейкоциты 6,0."


def test_run_on_diagnostic_copy_keeps_only_inserted_numeric_tail() -> None:
    assert extract_novel_clinical_text(
        "Анализ крови гемоглобин 120 лейкоциты 6 СРБ 12",
        "Анализ крови: гемоглобин 120, лейкоциты 6",
        duplicate_threshold=DIAGNOSTIC_DUPLICATE_THRESHOLD,
    ) == "СРБ 12"
