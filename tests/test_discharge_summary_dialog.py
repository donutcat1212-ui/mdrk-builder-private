from dataclasses import fields
from datetime import date, datetime

from mdrk_builder.domain import (
    DischargeSummaryDraft,
    ReviewIssue,
    ReviewSeverity,
)
from mdrk_builder.ui import discharge_summary_dialog as dialog_module
from mdrk_builder.ui.background_job import BackgroundJobRunner
from mdrk_builder.ui.discharge_summary_dialog import (
    DISCHARGE_TEXT_FIELDS,
    apply_discharge_form,
    blocking_discharge_issues,
    confirm_discharge_warnings,
)


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self._target = target

    def start(self) -> None:
        self._target()


class _ScheduledRoot:
    def __init__(self) -> None:
        self.callbacks = []

    def after(self, _delay: int, callback) -> None:
        self.callbacks.append(callback)


def _draft(tmp_path) -> DischargeSummaryDraft:
    return DischargeSummaryDraft(folder=tmp_path)


def test_form_exposes_every_editable_string_field(tmp_path) -> None:
    draft = _draft(tmp_path)
    model_string_fields = {
        field.name for field in fields(draft) if isinstance(getattr(draft, field.name), str)
    }

    assert {field.name for field in DISCHARGE_TEXT_FIELDS} == model_string_fields


def test_apply_form_keeps_source_identity_and_dates_read_only(tmp_path) -> None:
    draft = _draft(tmp_path)
    draft.identity.full_name = "ПАЦИЕНТ ИЗ ИСТОЧНИКА"
    draft.identity.birth_date = date(1970, 3, 2)
    draft.identity.sex = "мужской"
    draft.identity.medical_record_number = "701/26"
    draft.admission_datetime = datetime(2026, 8, 10, 9, 15)
    draft.discharge_datetime = datetime(2026, 8, 17, 12, 30)

    apply_discharge_form(
        draft,
        {
            "header_text": "Исправленная шапка",
            "medications": "",
            "transfusions": "",
            "recommendations": "Ручная рекомендация",
        },
    )

    assert draft.identity.full_name == "ПАЦИЕНТ ИЗ ИСТОЧНИКА"
    assert draft.identity.birth_date == date(1970, 3, 2)
    assert draft.identity.sex == "мужской"
    assert draft.identity.medical_record_number == "701/26"
    assert draft.admission_datetime == datetime(2026, 8, 10, 9, 15)
    assert draft.discharge_datetime == datetime(2026, 8, 17, 12, 30)
    assert draft.medications == ""
    assert draft.transfusions == ""
    assert draft.recommendations == "Ручная рекомендация"


def test_manual_text_change_clears_only_that_fields_provenance(tmp_path) -> None:
    draft = _draft(tmp_path)
    header_source = tmp_path / "current-discharge.docx"
    recommendation_source = tmp_path / "template.docx"
    draft.header_text = "Исходная шапка"
    draft.recommendations = "Шаблон рекомендаций"
    draft.field_sources = {
        "header_text": header_source,
        "recommendations": recommendation_source,
    }

    apply_discharge_form(
        draft,
        {
            "header_text": "Исправленная вручную шапка",
            "recommendations": "Шаблон рекомендаций",
        },
    )

    assert "header_text" not in draft.field_sources
    assert draft.field_sources["recommendations"] == recommendation_source


def test_blocking_issues_use_draft_contract(tmp_path) -> None:
    draft = _draft(tmp_path)
    issue = ReviewIssue(
        "missing_source",
        "Нет обязательного источника",
        ReviewSeverity.BLOCKING,
    )
    draft.issues.append(issue)

    assert blocking_discharge_issues(draft) == draft.blocking_issues() == (issue,)


def test_warnings_require_explicit_confirmation(monkeypatch, tmp_path) -> None:
    draft = _draft(tmp_path)
    draft.issues.append(
        ReviewIssue("needs_review", "Проверьте поле", ReviewSeverity.WARNING)
    )
    prompts: list[str] = []
    monkeypatch.setattr(
        dialog_module.messagebox,
        "askyesno",
        lambda _title, message, **_kwargs: prompts.append(message) or True,
    )

    assert confirm_discharge_warnings(object(), draft)
    assert prompts and "предупреждений: 1" in prompts[0]


def test_background_job_returns_to_scheduled_ui_callback() -> None:
    root = _ScheduledRoot()
    calls: list[tuple[object | None, Exception | None]] = []
    runner = BackgroundJobRunner(root, thread_factory=_ImmediateThread)

    runner.start(lambda: 42, lambda value, error: calls.append((value, error)), thread_name="test")

    assert runner.busy
    assert calls == []
    root.callbacks.pop(0)()
    assert not runner.busy
    assert calls == [(42, None)]


def test_background_job_delivers_worker_error_on_ui_callback() -> None:
    root = _ScheduledRoot()
    calls: list[tuple[object | None, Exception | None]] = []
    runner = BackgroundJobRunner(root, thread_factory=_ImmediateThread)

    def fail() -> int:
        raise RuntimeError("synthetic failure")

    runner.start(fail, lambda value, error: calls.append((value, error)), thread_name="test")
    root.callbacks.pop(0)()

    assert calls[0][0] is None
    assert isinstance(calls[0][1], RuntimeError)
    assert str(calls[0][1]) == "synthetic failure"
