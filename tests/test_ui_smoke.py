from docx import Document

import mdrk_builder.ui.app as app_module
from mdrk_builder.ui.app import _generate_smoke_document


def test_packaged_smoke_path_generates_docx(tmp_path) -> None:
    output = _generate_smoke_document(tmp_path)

    assert output.is_file()
    assert Document(output).paragraphs


def test_packaged_ci_smoke_does_not_initialize_tk(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "_generate_smoke_document",
        lambda directory: directory / "smoke-output.docx",
    )
    monkeypatch.setattr(
        app_module.tk,
        "Tk",
        lambda: (_ for _ in ()).throw(AssertionError("Tk must not start in CI smoke")),
    )

    assert app_module._run_smoke(include_ui=False) == 0


def test_packaged_ui_smoke_flag_constructs_ui(monkeypatch) -> None:
    calls: list[bool] = []

    def fake_smoke_test(*, include_ui: bool = False) -> int:
        calls.append(include_ui)
        return 0

    monkeypatch.setattr(app_module, "smoke_test", fake_smoke_test)

    assert app_module.main(["--smoke-test-ui"]) == 0
    assert calls == [True]


def test_packaged_smoke_writes_failure_report(monkeypatch, tmp_path) -> None:
    report = tmp_path / "smoke-report.txt"

    def fail_smoke(_directory):
        raise RuntimeError("synthetic packaged failure")

    monkeypatch.setattr(app_module, "_generate_smoke_document", fail_smoke)
    monkeypatch.setenv("MDRK_BUILDER_SMOKE_REPORT", str(report))

    assert app_module._run_smoke(include_ui=False) == 1
    report_text = report.read_text(encoding="utf-8")
    assert "phase=start" in report_text
    assert "synthetic packaged failure" in report_text


def test_gui_startup_failure_is_logged_and_shown_in_russian(monkeypatch, tmp_path) -> None:
    class FakeRoot:
        destroyed = False

        def destroy(self) -> None:
            self.destroyed = True

    root = FakeRoot()
    messages: list[tuple[str, str]] = []
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(app_module.tk, "Tk", lambda: root)
    monkeypatch.setattr(
        app_module,
        "MdrkBuilderApp",
        lambda _root: (_ for _ in ()).throw(RuntimeError("synthetic startup failure")),
    )
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda title, message, **_kwargs: messages.append((title, message)),
    )

    assert app_module._run_gui() == 1
    report = tmp_path / "MDRK Builder" / "logs" / "startup-error.log"
    assert "synthetic startup failure" in report.read_text(encoding="utf-8")
    assert messages and messages[0][0] == "Ошибка запуска МДРК Builder"
    assert str(report) in messages[0][1]
    assert root.destroyed
