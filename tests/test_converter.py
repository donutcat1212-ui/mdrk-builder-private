import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest

from mdrk_builder.infrastructure import converter as converter_module
from mdrk_builder.infrastructure.converter import (
    ConversionError,
    DocumentNormalizer,
    WindowsWordConverter,
)


def test_docx_only_normalizer_does_not_resolve_platform_converter(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"already normalized")

    def fail_if_called():
        raise AssertionError("default_converter must stay lazy for DOCX")

    monkeypatch.setattr(converter_module, "default_converter", fail_if_called)

    with DocumentNormalizer() as normalizer:
        assert normalizer.normalize(source) == source


def test_legacy_converter_is_resolved_once_and_closed(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.doc"
    source.write_bytes(b"legacy")
    calls = {"factory": 0, "close": 0}

    class FakeConverter:
        def convert(self, _source: Path, target: Path) -> Path:
            target.write_bytes(b"normalized")
            return target

        def close(self) -> None:
            calls["close"] += 1

    def factory() -> FakeConverter:
        calls["factory"] += 1
        return FakeConverter()

    monkeypatch.setattr(converter_module, "default_converter", factory)

    with DocumentNormalizer() as normalizer:
        first = normalizer.normalize(source)
        second = normalizer.normalize(source)
        assert first.read_bytes() == b"normalized"
        assert second == first

    assert calls == {"factory": 1, "close": 1}


def test_word_start_secures_word_before_open_and_suppresses_default_app_warning(
    monkeypatch, tmp_path: Path
) -> None:
    calls = {"co_initialize": 0, "dispatch": []}
    security_at_open: list[int] = []
    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: calls.__setitem__(  # type: ignore[attr-defined]
        "co_initialize", calls["co_initialize"] + 1
    )
    pythoncom.CoUninitialize = lambda: None  # type: ignore[attr-defined]

    class FakeDocument:
        def SaveAs2(self, path: str, **_kwargs) -> None:
            Path(path).write_bytes(b"normalized")

        def Close(self, **_kwargs) -> None:
            return None

    word = SimpleNamespace(
        AutomationSecurity=0,
        Visible=True,
        DisplayAlerts=1,
        Options=SimpleNamespace(AlertIfNotDefault=True),
    )
    word.Documents = SimpleNamespace(
        Open=lambda *_args, **_kwargs: (
            security_at_open.append(word.AutomationSecurity) or FakeDocument()
        )
    )
    client = ModuleType("win32com.client")

    def dispatch(name: str):
        calls["dispatch"].append(name)
        return word

    client.DispatchEx = dispatch  # type: ignore[attr-defined]
    win32com = ModuleType("win32com")
    win32com.__path__ = []  # type: ignore[attr-defined]
    win32com.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(
        WindowsWordConverter,
        "_process_id_for_word",
        staticmethod(lambda _word: 2468),
    )

    converter = WindowsWordConverter()
    source = tmp_path / "source.doc"
    destination = tmp_path / "destination.docx"
    source.write_bytes(b"legacy")

    assert converter.convert(source, destination) == destination
    expected_security = WindowsWordConverter.MSO_AUTOMATION_SECURITY_FORCE_DISABLE
    assert security_at_open == [expected_security]
    assert word.AutomationSecurity == expected_security
    assert word.Visible is False
    assert word.DisplayAlerts == 0
    assert word.Options.AlertIfNotDefault is False
    assert converter._word_pid == 2468
    assert calls == {"co_initialize": 1, "dispatch": ["Word.Application"]}


def test_word_setup_failure_closes_captured_instance(monkeypatch) -> None:
    calls = {"co_initialize": 0, "co_uninitialize": 0, "quit": 0, "killed": []}
    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: calls.__setitem__(  # type: ignore[attr-defined]
        "co_initialize", calls["co_initialize"] + 1
    )
    pythoncom.CoUninitialize = lambda: calls.__setitem__(  # type: ignore[attr-defined]
        "co_uninitialize", calls["co_uninitialize"] + 1
    )

    class FailingOptions:
        @property
        def AlertIfNotDefault(self) -> bool:
            return True

        @AlertIfNotDefault.setter
        def AlertIfNotDefault(self, _value: bool) -> None:
            raise RuntimeError("default-app modal blocked setup")

    class FailingWord:
        Visible = True
        DisplayAlerts = 1
        Options = FailingOptions()

        def Quit(self, **_kwargs) -> None:
            calls["quit"] += 1
            raise RuntimeError("modal blocked Quit")

    word = FailingWord()
    client = ModuleType("win32com.client")
    client.DispatchEx = lambda _name: word  # type: ignore[attr-defined]
    win32com = ModuleType("win32com")
    win32com.__path__ = []  # type: ignore[attr-defined]
    win32com.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(
        WindowsWordConverter,
        "_process_id_for_word",
        staticmethod(lambda _word: 2468),
    )
    converter = WindowsWordConverter()
    monkeypatch.setattr(
        converter,
        "_force_kill_word_process",
        lambda process_id: calls["killed"].append(process_id),
    )

    with pytest.raises(ConversionError, match="Word не удалось подготовить"):
        converter._start()

    assert calls == {
        "co_initialize": 1,
        "co_uninitialize": 1,
        "quit": 1,
        "killed": [2468],
    }
    assert converter._word is None
    assert converter._word_pid is None
    assert converter._pythoncom is None


def test_word_dispatch_failure_is_normalized_and_uninitializes_com(monkeypatch) -> None:
    calls = {"co_uninitialize": 0}
    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None  # type: ignore[attr-defined]
    pythoncom.CoUninitialize = lambda: calls.__setitem__(  # type: ignore[attr-defined]
        "co_uninitialize", calls["co_uninitialize"] + 1
    )
    client = ModuleType("win32com.client")

    def fail_dispatch(_name: str):
        raise RuntimeError("Word startup failed")

    client.DispatchEx = fail_dispatch  # type: ignore[attr-defined]
    win32com = ModuleType("win32com")
    win32com.__path__ = []  # type: ignore[attr-defined]
    win32com.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)

    with pytest.raises(ConversionError, match="Word не удалось запустить"):
        WindowsWordConverter()._start()

    assert calls["co_uninitialize"] == 1


def test_word_document_close_failure_does_not_mask_success(tmp_path: Path) -> None:
    source = tmp_path / "legacy.doc"
    destination = tmp_path / "normalized.docx"
    source.write_bytes(b"legacy")

    class FakeDocument:
        def SaveAs2(self, path: str, **_kwargs) -> None:
            Path(path).write_bytes(b"valid docx")

        def Close(self, **_kwargs) -> None:
            raise RuntimeError("Open.Close failed")

    class FakeDocuments:
        def Open(self, *_args, **_kwargs) -> FakeDocument:
            return FakeDocument()

    converter = WindowsWordConverter()
    converter._word = SimpleNamespace(Documents=FakeDocuments())

    assert converter.convert(source, destination) == destination
    assert destination.read_bytes() == b"valid docx"


def test_word_quit_exception_is_cleanup_only() -> None:
    calls = {"quit": 0, "co_uninitialize": 0}

    class FailingWord:
        def Quit(self, **_kwargs) -> None:
            calls["quit"] += 1
            raise RuntimeError("Quit failed")

    pythoncom = SimpleNamespace(
        CoUninitialize=lambda: calls.__setitem__(
            "co_uninitialize", calls["co_uninitialize"] + 1
        )
    )
    converter = WindowsWordConverter()
    converter._word = FailingWord()
    converter._pythoncom = pythoncom

    converter.close()

    assert calls == {"quit": 1, "co_uninitialize": 1}
    assert converter._word is None
    assert converter._pythoncom is None


def test_hung_word_quit_triggers_watchdog_for_captured_pid(monkeypatch) -> None:
    release_quit = threading.Event()
    killed: list[int] = []

    class HungWord:
        def Quit(self, **_kwargs) -> None:
            if not release_quit.wait(1):
                raise AssertionError("watchdog did not release hung Word")

    converter = WindowsWordConverter()
    converter.WORD_QUIT_TIMEOUT_SECONDS = 0.01
    converter._word = HungWord()
    converter._word_pid = 4321

    def force_kill(process_id: int) -> None:
        killed.append(process_id)
        release_quit.set()

    monkeypatch.setattr(converter, "_force_kill_word_process", force_kill)

    converter.close()

    assert killed == [4321]


def test_word_force_kill_targets_pid_not_image_name(monkeypatch) -> None:
    recorded: list[tuple[list[str], dict]] = []

    def run(command: list[str], **kwargs):
        recorded.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(converter_module.subprocess, "run", run)
    converter = WindowsWordConverter()

    converter._force_kill_word_process(9876)

    assert recorded[0][0] == ["taskkill", "/PID", "9876", "/F"]
    assert "/IM" not in recorded[0][0]
    assert recorded[0][1]["timeout"] == converter.WORD_KILL_TIMEOUT_SECONDS


def test_normalizer_close_ignores_converter_cleanup_failure() -> None:
    class FailingConverter:
        def convert(self, _source: Path, _destination: Path) -> Path:
            raise ConversionError("not used")

        def close(self) -> None:
            raise RuntimeError("cleanup failed")

    normalizer = DocumentNormalizer(converter=FailingConverter())

    normalizer.close()
