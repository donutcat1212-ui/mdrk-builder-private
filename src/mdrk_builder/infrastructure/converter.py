from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Protocol


class ConversionError(RuntimeError):
    pass


class LegacyConverter(Protocol):
    def convert(self, source: Path, destination: Path) -> Path: ...

    def close(self) -> None: ...


class WindowsWordConverter:
    """Interactive desktop Word adapter; imported only on Windows."""

    WD_FORMAT_DOCX = 16
    WD_DO_NOT_SAVE_CHANGES = 0
    MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3
    WORD_QUIT_TIMEOUT_SECONDS = 5.0
    WORD_KILL_TIMEOUT_SECONDS = 5.0

    def __init__(self) -> None:
        self._pythoncom = None
        self._word = None
        self._word_pid: int | None = None

    @staticmethod
    def _process_id_for_word(word) -> int | None:
        """Return the PID for this exact Word application window, when available."""
        try:
            window_handle = int(word.Hwnd)
            process_id = ctypes.c_ulong()
            result = ctypes.windll.user32.GetWindowThreadProcessId(  # type: ignore[attr-defined]
                window_handle,
                ctypes.byref(process_id),
            )
            value = int(process_id.value)
            if not result or value <= 0 or value == os.getpid():
                return None
            return value
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def _force_kill_word_process(self, process_id: int) -> None:
        """Terminate only the Word process created by DispatchEx."""
        if process_id <= 0 or process_id == os.getpid():
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process_id), "/F"],
                capture_output=True,
                text=True,
                timeout=self.WORD_KILL_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            # Cleanup must never turn an otherwise completed scan into a failure.
            return

    def _watch_for_hung_word(self, finished: threading.Event, process_id: int) -> None:
        if not finished.wait(self.WORD_QUIT_TIMEOUT_SECONDS):
            self._force_kill_word_process(process_id)

    def _start(self):
        if self._word is not None:
            return self._word
        try:
            import pythoncom  # type: ignore[import-not-found]
            import win32com.client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConversionError("Для чтения DOC/RTF требуется pywin32 и Microsoft Word") from exc
        pythoncom.CoInitialize()
        self._pythoncom = pythoncom
        try:
            word = win32com.client.DispatchEx("Word.Application")
        except Exception as exc:
            self.close()
            raise ConversionError(f"Word не удалось запустить: {exc}") from exc
        self._word = word
        self._word_pid = self._process_id_for_word(word)
        try:
            word.AutomationSecurity = self.MSO_AUTOMATION_SECURITY_FORCE_DISABLE
            word.Visible = False
            word.DisplayAlerts = 0
            # This suppresses Word's own "not the default app" prompt. It does not
            # change Windows file associations or make Word the default application.
            word.Options.AlertIfNotDefault = False
        except Exception as exc:
            self.close()
            raise ConversionError(f"Word не удалось подготовить: {exc}") from exc
        return word

    def convert(self, source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        word = self._start()
        document = None
        try:
            document = word.Documents.Open(
                str(source.resolve()),
                ConfirmConversions=False,
                ReadOnly=True,
                AddToRecentFiles=False,
                Visible=False,
                OpenAndRepair=True,
                NoEncodingDialog=True,
            )
            document.SaveAs2(
                str(destination.resolve()),
                FileFormat=self.WD_FORMAT_DOCX,
                AddToRecentFiles=False,
            )
        except Exception as exc:
            raise ConversionError(f"Word не смог преобразовать {source.name}: {exc}") from exc
        finally:
            if document is not None:
                try:
                    document.Close(SaveChanges=self.WD_DO_NOT_SAVE_CHANGES)
                except Exception:
                    # SaveAs2 is the meaningful operation. A COM cleanup error
                    # must not mask a successfully written output document.
                    pass
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ConversionError(f"Word не создал DOCX для {source.name}")
        return destination

    def close(self) -> None:
        word = self._word
        process_id = self._word_pid
        pythoncom = self._pythoncom
        self._word = None
        self._word_pid = None
        self._pythoncom = None

        finished = threading.Event()
        watchdog: threading.Thread | None = None
        try:
            if word is not None:
                if process_id is not None:
                    watchdog = threading.Thread(
                        target=self._watch_for_hung_word,
                        args=(finished, process_id),
                        name="mdrk-word-quit-watchdog",
                        daemon=True,
                    )
                    watchdog.start()
                try:
                    word.Quit(SaveChanges=self.WD_DO_NOT_SAVE_CHANGES)
                except Exception:
                    if process_id is not None:
                        self._force_kill_word_process(process_id)
        finally:
            finished.set()
            if watchdog is not None:
                watchdog.join(timeout=0.2)
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass


class LibreOfficeConverter:
    """Development adapter for macOS/Linux; production Windows uses Word."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("soffice") or ""
        if not self.executable:
            raise ConversionError("LibreOffice/soffice не найден")

    def convert(self, source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(prefix="mdrk-lo-profile-") as profile_dir,
            tempfile.TemporaryDirectory(prefix="mdrk-lo-output-", dir=destination.parent) as output_dir,
        ):
            command = [
                self.executable,
                "--headless",
                f"-env:UserInstallation=file://{profile_dir}",
                "--convert-to",
                "docx",
                "--outdir",
                output_dir,
                str(source.resolve()),
            ]
            environment = {**os.environ, "HOME": profile_dir, "TMPDIR": tempfile.gettempdir()}
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                env=environment,
                check=False,
            )
            produced_files = list(Path(output_dir).glob("*.docx"))
            if completed.returncode != 0 or len(produced_files) != 1:
                detail = (completed.stderr or completed.stdout).strip()
                raise ConversionError(f"LibreOffice не смог преобразовать {source.name}: {detail}")
            if destination.exists():
                destination.unlink()
            shutil.copy2(produced_files[0], destination)
        return destination

    def close(self) -> None:
        return None


def default_converter() -> LegacyConverter:
    if platform.system() == "Windows":
        return WindowsWordConverter()
    return LibreOfficeConverter()


class DocumentNormalizer:
    SUPPORTED = {".docx", ".doc", ".rtf"}

    def __init__(self, converter: LegacyConverter | None = None) -> None:
        # DOCX is already normalized and must not require Word/LibreOffice.
        # Resolve the platform converter only when the first legacy file occurs.
        self.converter = converter
        self._temporary = tempfile.TemporaryDirectory(prefix="mdrk-normalized-")
        self.directory = Path(self._temporary.name)

    def normalize(self, source: Path) -> Path:
        suffix = source.suffix.casefold()
        if suffix not in self.SUPPORTED:
            raise ConversionError(f"Неподдерживаемый формат: {source.suffix}")
        if suffix == ".docx":
            return source
        if self.converter is None:
            self.converter = default_converter()
        safe_name = f"{abs(hash(str(source.resolve()))):x}-{source.stem}.docx"
        return self.converter.convert(source, self.directory / safe_name)

    def close(self) -> None:
        try:
            if self.converter is not None:
                try:
                    self.converter.close()
                except Exception:
                    # Cleanup errors must not discard an already assembled scan.
                    pass
        finally:
            try:
                self._temporary.cleanup()
            except OSError:
                pass

    def __enter__(self) -> "DocumentNormalizer":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
