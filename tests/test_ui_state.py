import queue
from datetime import datetime

from mdrk_builder.domain import Episode, MdrkKind
from mdrk_builder.ui import app as app_module
from mdrk_builder.ui.app import MdrkBuilderApp


class _Variable:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Text:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self, _start: str, _end: str) -> str:
        return self.value

    def delete(self, _start: str, _end: str) -> None:
        self.value = ""


class _Tree:
    def __init__(self) -> None:
        self.rows = ["row"]

    def get_children(self) -> tuple[str, ...]:
        return tuple(self.rows)

    def delete(self, *_rows: str) -> None:
        self.rows.clear()


class _Button:
    def __init__(self) -> None:
        self.state = ""

    def configure(self, *, state: str) -> None:
        self.state = state


class _Root:
    def after(self, _delay: int, _callback) -> None:
        return None


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


def _entry_values(meeting: str) -> dict[str, _Variable]:
    return {
        "full_name": _Variable("Тестов Тест Тестович"),
        "record_number": _Variable("123"),
        "birth_date": _Variable("01.01.2000"),
        "sex": _Variable("мужской"),
        "admission": _Variable("09.08.2026 12:00"),
        "meeting": _Variable(meeting),
        "department": _Variable("ОМР"),
        "stage": _Variable("2 этап"),
        "duration": _Variable("14"),
    }


def test_invalid_meeting_keeps_current_snapshot_and_text(monkeypatch, tmp_path) -> None:
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app.episode.initial_meeting_at = datetime(2026, 8, 10, 8, 0)
    app.episode.initial_sections.clinical_diagnosis = "сохранённый"
    app._current_kind = MdrkKind.INITIAL
    app.kind_var = _Variable(MdrkKind.FINAL.value)
    app.status_var = _Variable()
    app._last_form_error = ""
    app._entry_variables = _entry_values("10.08.2026")
    app._text_fields = {"clinical_diagnosis": _Text("не потерять")}
    errors: list[str] = []
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda _title, message: errors.append(message),
    )

    app._on_kind_changed()

    assert app._current_kind is MdrkKind.INITIAL
    assert app.kind_var.get() == MdrkKind.INITIAL.value
    assert app._entry_variables["meeting"].get() == "10.08.2026"
    assert app._text_fields["clinical_diagnosis"].value == "не потерять"
    assert app.episode.initial_sections.clinical_diagnosis == "сохранённый"
    assert errors and "Время заседания" in errors[0]


def test_changed_meeting_blocks_generation_form_apply_without_losing_edits(
    monkeypatch,
    tmp_path,
) -> None:
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app.episode.identity.full_name = "Сохранённый Пациент Тестович"
    app.episode.initial_meeting_at = datetime(2026, 8, 10, 8)
    app.episode.initial_sections.clinical_diagnosis = "сохранённый"
    app._current_kind = MdrkKind.INITIAL
    app.status_var = _Variable()
    app._last_form_error = ""
    app._entry_variables = _entry_values("10.08.2026 09:00")
    app._text_fields = {"clinical_diagnosis": _Text("не потерять")}
    errors: list[str] = []
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda _title, message: errors.append(message),
    )

    assert not app._apply_form()
    assert app.episode.initial_meeting_at == datetime(2026, 8, 10, 8)
    assert app.episode.identity.full_name == "Сохранённый Пациент Тестович"
    assert app.episode.initial_sections.clinical_diagnosis == "сохранённый"
    assert app._text_fields["clinical_diagnosis"].value == "не потерять"
    assert errors and "Сканировать" in errors[0]


def test_changed_meeting_blocks_snapshot_switch(monkeypatch, tmp_path) -> None:
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app.episode.initial_meeting_at = datetime(2026, 8, 10, 8)
    app.episode.initial_sections.clinical_diagnosis = "сохранённый"
    app._current_kind = MdrkKind.INITIAL
    app.kind_var = _Variable(MdrkKind.FINAL.value)
    app.status_var = _Variable()
    app._last_form_error = ""
    app._entry_variables = _entry_values("10.08.2026 09:00")
    app._text_fields = {"clinical_diagnosis": _Text("не потерять")}
    errors: list[str] = []
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda _title, message: errors.append(message),
    )

    app._on_kind_changed()

    assert app._current_kind is MdrkKind.INITIAL
    assert app.kind_var.get() == MdrkKind.INITIAL.value
    assert app.episode.initial_meeting_at == datetime(2026, 8, 10, 8)
    assert app.episode.initial_sections.clinical_diagnosis == "сохранённый"
    assert app._text_fields["clinical_diagnosis"].value == "не потерять"
    assert errors and "Сканировать" in errors[0]


def test_rescan_passes_both_meeting_boundaries_and_replaces_edited_kind(
    monkeypatch,
    tmp_path,
) -> None:
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app.episode.initial_meeting_at = datetime(2026, 8, 10, 8)
    app.episode.final_meeting_at = datetime(2026, 8, 20, 11)
    app._current_kind = MdrkKind.FINAL
    app._scanning = False
    app._scan_results = queue.Queue()
    app._scan_thread = None
    app._scan_folder = None
    app._entry_variables = {"meeting": _Variable("19.08.2026 15:30")}
    app.folder_var = _Variable(str(tmp_path))
    app.status_var = _Variable()
    app.root = _Root()
    app._invalidate_episode = lambda: setattr(app, "episode", None)
    app._set_folder_field = lambda value: app.folder_var.set(value)
    app._set_scanning = lambda value: setattr(app, "_scanning", value)
    captured: dict[str, object] = {}
    confirmations: list[str] = []

    def fake_scan(folder, **kwargs) -> Episode:
        captured["folder"] = folder
        captured.update(kwargs)
        return Episode(folder=folder)

    monkeypatch.setattr(app_module, "scan_patient_folder", fake_scan)
    monkeypatch.setattr(app_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda _title, message: confirmations.append(message) or True,
    )

    app._start_scan()

    assert confirmations and "ручные правки" in confirmations[0].casefold()
    assert captured == {
        "folder": tmp_path,
        "initial_meeting_at": datetime(2026, 8, 10, 8),
        "final_meeting_at": datetime(2026, 8, 19, 15, 30),
    }


def test_folder_edit_invalidates_loaded_episode(tmp_path) -> None:
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app._setting_folder_field = False
    app._last_form_error = ""
    app._scanning = False
    app._entry_variables = {"full_name": _Variable("Старый пациент")}
    app._text_fields = {"clinical_diagnosis": _Text("Старый диагноз")}
    app.folder_var = _Variable(str(tmp_path / "other"))
    app.status_var = _Variable()
    app.scan_button = _Button()
    app.generate_button = _Button()
    trees = [_Tree() for _ in range(6)]
    (
        app.source_tree,
        app.icf_tree,
        app.scale_tree,
        app.procedure_tree,
        app.finding_tree,
        app.issue_tree,
    ) = trees
    app._scale_refs = [(0, 0)]

    app._on_folder_field_changed()

    assert app.episode is None
    assert app.generate_button.state == "disabled"
    assert app._entry_variables["full_name"].get() == ""
    assert all(not tree.rows for tree in trees)
    assert "сканирование заново" in app.status_var.get()
