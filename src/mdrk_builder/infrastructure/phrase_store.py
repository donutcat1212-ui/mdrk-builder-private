"""Portable, versioned physician phrases; independent of application versions."""
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from .file_lock import InterprocessFileLock


def user_phrases_path() -> Path:
    if getattr(sys, "frozen", False):
        directory = Path(sys.executable).resolve().parent
    else:
        directory = Path(__file__).resolve().parents[3]
    return directory / "user_phrases.json"


class PhraseStorageError(RuntimeError):
    """The user's phrase file could not be read or safely changed."""


class PhraseStore:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else user_phrases_path()

    def phrases(self, field: str) -> tuple[str, ...]:
        return tuple(self._load()["fields"].get(field, ()))

    def add(self, field: str, text: str) -> None:
        if not text.strip():
            raise ValueError("Введите текст фразы")
        self._change(field, text, remove=False)

    def delete(self, field: str, text: str) -> None:
        self._change(field, text, remove=True)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"schema_version": 1, "fields": {}}
        except (OSError, ValueError) as exc:
            raise PhraseStorageError(f"Не удалось прочитать файл фраз: {self.path}\n{exc}") from exc
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise PhraseStorageError(f"Неподдерживаемая версия файла фраз: {self.path}. Файл сохранён без изменений.")
        fields = data.get("fields")
        if not isinstance(fields, dict) or any(
            not isinstance(values, list) or any(not isinstance(text, str) for text in values)
            for values in fields.values()
        ):
            raise PhraseStorageError(f"Неверный формат списка фраз: {self.path}. Файл сохранён без изменений.")
        return data

    def _change(self, field: str, text: str, *, remove: bool) -> None:
        lock = InterprocessFileLock(self.path.with_suffix(".json.lock"))
        temporary = None
        try:
            if not lock.acquire(timeout=1.0):
                raise PhraseStorageError("Файл фраз занят другим пользователем. Повторите сохранение.")
            # Reload under the lock: another running copy may have added a phrase.
            data = self._load()
            phrases = data["fields"].setdefault(field, [])
            if remove:
                if text not in phrases:
                    return
                phrases.remove(text)
            elif text not in phrases:
                phrases.append(text)
            else:
                return
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                    prefix=".user_phrases-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise PhraseStorageError(f"Не удалось сохранить фразы: {self.path}\n{exc}") from exc
        finally:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            finally:
                lock.release()
