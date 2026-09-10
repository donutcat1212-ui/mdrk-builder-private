from datetime import datetime

import pytest

from mdrk_builder.application.extractors import extract_conclusion, extract_scale_measurements
from mdrk_builder.application.snapshot import build_snapshot
from mdrk_builder.application.discharge_summary import _project_team_findings
from mdrk_builder.domain import Episode, MdrkKind, SpecialistFinding, SpecialistRole
from mdrk_builder.infrastructure.draft_store import decode, encode
from test_extractors import _document


def test_physical_therapy_outcome_preserves_list_without_copying_procedure_program():
    value = extract_conclusion(_document(
        "За время проведения реабилитационных мероприятий отмечается положительная динамика в виде:\n"
        "УВЕЛИЧЕНИЕ_ТОЛЕРАНТНОСТИ;\nУЛУЧШЕНИЕ_САМООБСЛУЖИВАНИЯ.\n"
        "С пациентом проводились занятия согласно индивидуальной программы:\n"
        "ПРОЦЕДУРА\nСпециалист по физической реабилитации СОТРУДНИК А.А."
    ), SpecialistRole.PHYSICAL_THERAPIST)
    assert "УВЕЛИЧЕНИЕ_ТОЛЕРАНТНОСТИ;\nУЛУЧШЕНИЕ_САМООБСЛУЖИВАНИЯ." in value
    assert "ПРОЦЕДУРА" not in value and "СОТРУДНИК" not in value


def test_neuropsych_course_preserves_negative_qualification_and_both_paragraphs():
    value = extract_conclusion(_document(
        "Нейропсихологический статус: ИСХОДНЫЙ\n"
        "На основании данных обследования рекомендовано: ПЛАН\nЗадача 1: ЗАДАЧА\n"
        "На основании данных обследования проведена коррекция: ВЫПОЛНЕНО\n"
        "Отмечается следующая положительная динамика:\n"
        "Значимой положительной динамики не отмечается. ПЕРВЫЙ_АБЗАЦ.\n"
        "ВТОРОЙ_АБЗАЦ.\nМедицинский психолог СОТРУДНИК А.А."
    ), SpecialistRole.NEUROPSYCHOLOGIST)
    assert all(text in value for text in ("ВЫПОЛНЕНО", "не отмечается", "ПЕРВЫЙ_АБЗАЦ", "ВТОРОЙ_АБЗАЦ"))
    assert all(text not in value for text in ("ИСХОДНЫЙ", "ПЛАН", "ЗАДАЧА", "СОТРУДНИК"))


def test_old_course_outcome_does_not_override_later_examination():
    value = extract_conclusion(_document(
        "Нейропсихологический статус: OLD\nОтмечается улучшение: OLD_OUTCOME\n"
        "Подпись: FIRST\nНейропсихологический статус: CURRENT\n"
        "Рекомендовано: CURRENT_ADVICE\nПодпись: SECOND"
    ), SpecialistRole.NEUROPSYCHOLOGIST)
    assert "OLD" not in value and "CURRENT_ADVICE" in value


def test_speech_unchanged_status_expands_same_examination_and_retains_recommendation():
    value = extract_conclusion(_document(
        "Логопедический статус:\nРАЗВЁРНУТЫЙ_СТАТУС\nДРУГОЙ_АБЗАЦ\n"
        "Шкала: 42\nЗадача на этап МР: ПЛАН\n"
        "Динамика: НЕЗНАЧИТЕЛЬНОЕ_УЛУЧШЕНИЕ\n"
        "Рекомендовано продолжить занятия.\nЛогопедический статус при выписке: прежний.\n"
        "Медицинский логопед СОТРУДНИК А.А."
    ), SpecialistRole.LOGOPEDIST)
    assert all(text in value for text in ("РАЗВЁРНУТЫЙ_СТАТУС", "ДРУГОЙ_АБЗАЦ", "НЕЗНАЧИТЕЛЬНОЕ_УЛУЧШЕНИЕ", "Рекомендовано продолжить"))
    assert all(text not in value for text in ("прежний", "ПЛАН", "Шкала", "СОТРУДНИК"))


@pytest.mark.parametrize('separator,repeat_label', [('–', 'повт. –'), ('-', 'повторно:'), (':', 'повторная оценка –')])
def test_speech_narrative_pair_reaches_discharge_with_unknown_baseline_date(tmp_path, separator, repeat_label):
    initial_at = datetime(2026, 8, 1, 10)
    final_at = datetime(2026, 8, 15, 10)
    document = _document(f'Шкала Вассермана Л.И. для оценки речевых нарушений {separator} 43 балла '
                         f'({repeat_label} 41 балл). Динамика: 2 балла.')
    scores = extract_scale_measurements(document, SpecialistRole.LOGOPEDIST, final_at)
    assert [score.value for score in scores] == ['43', '41']
    assert scores[0].measured_at is None
    assert scores[1].measured_at == final_at
    episode = Episode(tmp_path, admission_datetime=initial_at, initial_meeting_at=initial_at, final_meeting_at=final_at)
    episode.findings = [SpecialistFinding(SpecialistRole.LOGOPEDIST, 'STATUS', final_at, document.source_path, scores)]
    snapshot = build_snapshot(decode(encode(episode)), MdrkKind.FINAL)
    team = _project_team_findings(snapshot)
    assert len(team) == 1 and len(team[0].scales) == 1
    row = team[0].scales[0]
    assert row.initial_value == '43' and row.value == '41'
    assert row.initial_at is None and row.current_at == final_at
    assert row.initial_source == document.source_path
    assert not build_snapshot(episode, MdrkKind.INITIAL).scale_rows
def test_ownerless_structural_icf_copies_merge_without_losing_distinct_notes(tmp_path):
    from datetime import datetime
    from mdrk_builder.application.scanner import _merge_icf
    from mdrk_builder.domain import Episode, SpecialistRole
    from test_scanner import _record, _row, _icf_table
    initial = datetime(2026, 8, 1)
    final = datetime(2026, 8, 5)
    episode = Episode(tmp_path, initial_meeting_at=initial, final_meeting_at=final)
    rows = [_record("primary.docx", "", role=SpecialistRole.NEUROLOGIST, clinical_datetime=initial,
        tables=[_icf_table(_row({0: "s110", 1: "Структура головного мозга", 11: "2", 12: "КТ"}))]),
        _record("repeat.docx", "", role=SpecialistRole.NEUROPSYCHOLOGIST, document_type="follow_up", clinical_datetime=final,
        tables=[_icf_table(_row({0: "s110", 1: "Структура головного мозга", 11: "2", 12: "КТ"}),
                          _row({0: "b730", 1: "Сила", 11: "2", 12: "левая рука"}),
                          _row({0: "b730", 1: "Сила", 11: "3", 12: "правая рука"}))])]
    _merge_icf(episode, rows)
    structures = [r for r in episode.icf_domains if r.code == "s110"]
    assert len(structures) == 1
    assert structures[0].initial.value == structures[0].final.value == 2
    assert len([r for r in episode.icf_domains if r.code == "b730"]) == 2
