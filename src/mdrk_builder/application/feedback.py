from __future__ import annotations

import os
import platform
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


FEEDBACK_CATEGORIES = ("Ошибка", "Предложение", "Вопрос")
MAX_FEEDBACK_MESSAGE_LENGTH = 50_000
_PENDING_PATTERN = ".issues-pending-*.txt"
_RECORD_ID_PATTERN = re.compile(r"^=== КОНЕЦ ОТЗЫВА ([0-9a-f]{32}) ===$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class FeedbackSubmission:
    category: str
    message: str
    author: str = ""


@dataclass(frozen=True, slots=True)
class FeedbackSaveResult:
    record_id: str
    issues_path: Path
    queued: bool


class FeedbackStorageError(RuntimeError):
    """Raised when a feedback record cannot be persisted at all."""


def feedback_file_path() -> Path:
    """Return the shared issues file next to the EXE or in the source project."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "issues.txt"

    source_file = Path(__file__).resolve()
    for candidate in source_file.parents:
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "mdrk_builder"
        ).is_dir():
            return candidate / "issues.txt"
    return Path.cwd().resolve() / "issues.txt"


def save_feedback(
    submission: FeedbackSubmission,
    *,
    directory: Path | None = None,
    app_version: str,
    lock_timeout: float = 3.0,
) -> FeedbackSaveResult:
    """Durably stage a record, then merge it into the shared issues file.

    Every caller first creates a unique pending record in the same directory.
    Cooperating processes serialize the append with an OS file lock. If a
    network share cannot grant that lock promptly, the pending record remains
    intact and a later submission will merge it into ``issues.txt``.
    """

    normalized = _normalize_submission(submission)
    issues_path = (directory.resolve() / "issues.txt") if directory else feedback_file_path()
    record_id = uuid.uuid4().hex
    record = format_feedback_record(
        normalized,
        record_id=record_id,
        app_version=app_version,
    )
    pending_path = issues_path.parent / f".issues-pending-{record_id}.txt"

    try:
        _write_new_file(pending_path, record.encode("utf-8"))
    except OSError as exc:
        raise FeedbackStorageError(
            "Папка программы недоступна для записи обратной связи."
        ) from exc

    lock = _InterprocessFileLock(issues_path.with_name("issues.txt.lock"))
    try:
        acquired = lock.acquire(timeout=lock_timeout)
    except OSError:
        acquired = False
    if not acquired:
        return FeedbackSaveResult(record_id, issues_path, queued=pending_path.exists())

    try:
        try:
            _merge_pending_records(issues_path)
        except (OSError, UnicodeError):
            return FeedbackSaveResult(record_id, issues_path, queued=True)
    finally:
        lock.release()

    return FeedbackSaveResult(record_id, issues_path, queued=pending_path.exists())


def format_feedback_record(
    submission: FeedbackSubmission,
    *,
    record_id: str,
    app_version: str,
    created_at: datetime | None = None,
    os_name: str | None = None,
) -> str:
    normalized = _normalize_submission(submission)
    timestamp = (created_at or datetime.now().astimezone()).astimezone().isoformat(
        timespec="seconds"
    )
    system_name = os_name or " ".join(
        value for value in (platform.system(), platform.release()) if value
    )
    author = normalized.author or "не указан"
    return (
        f"=== ОТЗЫВ {record_id} ===\n"
        f"Время: {timestamp}\n"
        f"Версия программы: {app_version.strip() or 'не указана'}\n"
        f"ОС: {system_name or 'не определена'}\n"
        f"Тип: {normalized.category}\n"
        f"Имя/контакт: {author}\n"
        "Сообщение:\n"
        f"{normalized.message}\n"
        f"=== КОНЕЦ ОТЗЫВА {record_id} ===\n"
    )


def _normalize_submission(submission: FeedbackSubmission) -> FeedbackSubmission:
    category = submission.category.strip()
    if category not in FEEDBACK_CATEGORIES:
        raise ValueError("Выберите тип обратной связи")
    message = submission.message.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not message:
        raise ValueError("Введите текст сообщения")
    if len(message) > MAX_FEEDBACK_MESSAGE_LENGTH:
        raise ValueError(
            f"Сообщение слишком длинное: максимум {MAX_FEEDBACK_MESSAGE_LENGTH} символов"
        )
    author = " ".join(submission.author.split())
    return FeedbackSubmission(category=category, message=message, author=author)


def _write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _merge_pending_records(issues_path: Path) -> None:
    completed_ids = _completed_record_ids(issues_path)
    for pending_path in sorted(issues_path.parent.glob(_PENDING_PATTERN)):
        try:
            record = pending_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        record_ids = _RECORD_ID_PATTERN.findall(record)
        if len(record_ids) != 1:
            continue
        record_id = record_ids[0]
        if record_id not in completed_ids:
            _append_record(issues_path, record)
            completed_ids.add(record_id)
        try:
            pending_path.unlink()
        except OSError:
            # A complete record is already identifiable in issues.txt, so a
            # later merge can safely retry the cleanup without duplicating it.
            pass


def _completed_record_ids(issues_path: Path) -> set[str]:
    try:
        existing = issues_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return set(_RECORD_ID_PATTERN.findall(existing))


def _append_record(issues_path: Path, record: str) -> None:
    prefix = b""
    try:
        if issues_path.stat().st_size:
            prefix = b"\n"
    except FileNotFoundError:
        pass
    descriptor = os.open(issues_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _write_all(descriptor, prefix + record.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Не удалось записать обратную связь")
        remaining = remaining[written:]


class _InterprocessFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None
        self._locked = False

    def acquire(self, *, timeout: float) -> bool:
        self._file = self.path.open("a+b")
        try:
            self._ensure_lock_byte()
        except OSError:
            self._close()
            raise
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._try_lock()
                self._locked = True
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._close()
                    return False
                time.sleep(0.05)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if self._locked:
                self._unlock()
        finally:
            self._locked = False
            self._close()

    def _ensure_lock_byte(self) -> None:
        assert self._file is not None
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)

    def _try_lock(self) -> None:
        assert self._file is not None
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        assert self._file is not None
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
