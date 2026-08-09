from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
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

    def __init__(self) -> None:
        self._pythoncom = None
        self._word = None

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
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        self._word = word
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
                document.Close(SaveChanges=self.WD_DO_NOT_SAVE_CHANGES)
        if not destination.exists():
            raise ConversionError(f"Word не создал DOCX для {source.name}")
        return destination

    def close(self) -> None:
        try:
            if self._word is not None:
                self._word.Quit(SaveChanges=self.WD_DO_NOT_SAVE_CHANGES)
        finally:
            self._word = None
            if self._pythoncom is not None:
                self._pythoncom.CoUninitialize()
                self._pythoncom = None


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
                self.converter.close()
        finally:
            self._temporary.cleanup()

    def __enter__(self) -> "DocumentNormalizer":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
