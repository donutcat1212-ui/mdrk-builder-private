from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import mdrk_builder.application.feedback as feedback_module
import mdrk_builder.ui.app as app_module
from mdrk_builder.application.feedback import (
    FeedbackStorageError,
    FeedbackSubmission,
    feedback_file_path,
    format_feedback_record,
    save_feedback,
)


def test_feedback_record_contains_only_explicit_fields_and_safe_runtime_metadata(
    tmp_path,
) -> None:
    record = format_feedback_record(
        FeedbackSubmission(
            category="Ошибка",
            author="  Сотрудник   1  ",
            message="Кнопка не отвечает.\nПовторяется дважды.",
        ),
        record_id="a" * 32,
        app_version="1.0.0",
        created_at=datetime(2026, 8, 11, 10, 30, tzinfo=timezone.utc),
        os_name="Windows 11",
    )

    assert "Версия программы: 1.0.0" in record
    assert "ОС: Windows 11" in record
    assert "Тип: Ошибка" in record
    assert "Имя/контакт: Сотрудник 1" in record
    assert "Кнопка не отвечает.\nПовторяется дважды." in record
    assert str(tmp_path) not in record


def test_save_feedback_appends_to_issues_txt_and_removes_pending_record(tmp_path) -> None:
    result = save_feedback(
        FeedbackSubmission("Предложение", "Добавить горячую клавишу"),
        directory=tmp_path,
        app_version="1.0.0",
    )

    assert result.issues_path == tmp_path / "issues.txt"
    assert not result.queued
    content = result.issues_path.read_text(encoding="utf-8")
    assert "Добавить горячую клавишу" in content
    assert result.record_id in content
    assert not list(tmp_path.glob(".issues-pending-*.txt"))
    assert str(tmp_path) not in content


def test_busy_shared_file_keeps_unique_record_then_next_save_merges_it(
    monkeypatch,
    tmp_path,
) -> None:
    original_acquire = feedback_module._InterprocessFileLock.acquire
    with monkeypatch.context() as patch:
        patch.setattr(
            feedback_module._InterprocessFileLock,
            "acquire",
            lambda _self, *, timeout: False,
        )
        queued = save_feedback(
            FeedbackSubmission("Ошибка", "Первая запись"),
            directory=tmp_path,
            app_version="1.0.0",
        )

    assert queued.queued
    assert len(list(tmp_path.glob(".issues-pending-*.txt"))) == 1

    assert feedback_module._InterprocessFileLock.acquire is original_acquire
    merged = save_feedback(
        FeedbackSubmission("Вопрос", "Вторая запись"),
        directory=tmp_path,
        app_version="1.0.0",
    )

    assert not merged.queued
    content = (tmp_path / "issues.txt").read_text(encoding="utf-8")
    assert content.count("Первая запись") == 1
    assert content.count("Вторая запись") == 1
    assert not list(tmp_path.glob(".issues-pending-*.txt"))


def test_parallel_feedback_writers_do_not_lose_or_duplicate_messages(tmp_path) -> None:
    messages = [f"Сообщение {index:02d}" for index in range(12)]

    def write(message: str) -> None:
        save_feedback(
            FeedbackSubmission("Ошибка", message),
            directory=tmp_path,
            app_version="1.0.0",
            lock_timeout=5.0,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, messages))

    content = (tmp_path / "issues.txt").read_text(encoding="utf-8")
    for message in messages:
        assert content.count(message) == 1
    assert content.count("=== КОНЕЦ ОТЗЫВА ") == len(messages)
    assert not list(tmp_path.glob(".issues-pending-*.txt"))


def test_feedback_path_is_next_to_frozen_executable(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "MDRK_Builder.exe"
    monkeypatch.setattr(feedback_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(feedback_module.sys, "executable", str(executable))

    assert feedback_file_path() == tmp_path / "issues.txt"


def test_unwritable_location_raises_storage_error(tmp_path) -> None:
    missing_directory = tmp_path / "missing"

    try:
        save_feedback(
            FeedbackSubmission("Вопрос", "Текст"),
            directory=missing_directory,
            app_version="1.0.0",
        )
    except FeedbackStorageError:
        pass
    else:
        raise AssertionError("FeedbackStorageError was not raised")


def test_feedback_ui_sends_only_dialog_submission(monkeypatch) -> None:
    submission = FeedbackSubmission("Ошибка", "Только текст отзыва", "Контакт")
    captured: list[tuple[FeedbackSubmission, str]] = []

    class Dialog:
        def __init__(self, _root, *, initial=None) -> None:
            assert initial is None
            self.result = submission

    monkeypatch.setattr(app_module, "FeedbackDialog", Dialog)
    monkeypatch.setattr(
        app_module,
        "save_feedback",
        lambda value, *, app_version: (
            captured.append((value, app_version))
            or SimpleNamespace(queued=False)
        ),
    )
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)

    app = object.__new__(app_module.MdrkBuilderApp)
    app.root = object()
    app.episode = SimpleNamespace(patient="это не должно попасть в отзыв")
    app._show_feedback()

    assert captured == [(submission, app_module.__version__)]


def test_global_notice_and_about_text_cover_single_review_gate() -> None:
    assert "перед подписанием" in app_module.REVIEW_NOTICE_SHORT.casefold()
    assert "пропуски или ошибки" in app_module.REVIEW_NOTICE_SHORT.casefold()

    text = app_module.about_text().casefold()
    assert "автоматически переносит и форматирует" in text
    assert "перед подписанием" in text
    assert "не выполняет диагностику" in text
    assert "не назначает лечение" in text
    assert "«как есть»" in text
