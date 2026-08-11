import queue
from datetime import date, datetime
from types import SimpleNamespace

from mdrk_builder.application.validation import (
    acknowledge_issue,
    has_issue_acknowledgements,
    is_issue_acknowledged,
)
from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    IcfQualifier,
    MdrkKind,
    Procedure,
    ReviewIssue,
    ReviewSeverity,
    ScaleMeasurement,
    SpecialistFinding,
    SpecialistRole,
)
from mdrk_builder.ui import app as app_module
from mdrk_builder.ui import dialogs as dialogs_module
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


class _DataTree:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[object, ...]] = {}
        self.displaycolumns: tuple[str, ...] = ()

    def get_children(self) -> tuple[str, ...]:
        return tuple(self.rows)

    def delete(self, *rows: str) -> None:
        for row in rows:
            self.rows.pop(row, None)

    def configure(self, *, displaycolumns: tuple[str, ...]) -> None:
        self.displaycolumns = displaycolumns

    def insert(
        self,
        _parent: str,
        _position: str,
        *,
        iid: str,
        values: tuple[object, ...],
    ) -> None:
        self.rows[iid] = values


class _SelectableTree:
    def __init__(self, selected: str | tuple[str, ...]) -> None:
        self.selected = selected

    def selection(self) -> tuple[str, ...]:
        return self.selected if isinstance(self.selected, tuple) else (self.selected,)


class _KeyboardWidget:
    def __init__(self, widget_class: str = "TEntry") -> None:
        self.widget_class = widget_class
        self.generated_events: list[str] = []
        self.selection_range_args: tuple[object, ...] | None = None
        self.cursor: object | None = None

    def winfo_class(self) -> str:
        return self.widget_class

    def event_generate(self, event: str) -> None:
        self.generated_events.append(event)

    def selection_range(self, *args: object) -> None:
        self.selection_range_args = args

    def icursor(self, index: object) -> None:
        self.cursor = index


class _KeyboardTree:
    def __init__(self) -> None:
        self.selected: tuple[str, ...] = ("1",)
        self.clipboard = ""
        self.rows = {
            "0": ("a", "b"),
            "1": ("в", "г"),
        }

    def winfo_class(self) -> str:
        return "Treeview"

    def selection(self) -> tuple[str, ...]:
        return self.selected

    def selection_set(self, rows: tuple[str, ...]) -> None:
        self.selected = tuple(rows)

    def get_children(self) -> tuple[str, ...]:
        return tuple(self.rows)

    def item(self, item_id: str, option: str) -> tuple[str, ...]:
        assert option == "values"
        return self.rows[item_id]

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, value: str) -> None:
        self.clipboard += value


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


def test_procedure_edit_preserves_extracted_schedule_provenance(tmp_path) -> None:
    previous = Procedure(
        code="A19.23.001",
        name="ЛФК",
        specialist="Специалист",
        actual_count=2,
        duration_minutes=30,
        frequency="ежедневно",
        source=tmp_path / "назначения.docx",
        count_needs_review=True,
        performed_dates=(date(2026, 8, 4), date(2026, 8, 5)),
    )
    dialog = object.__new__(dialogs_module.ProcedureDialog)
    dialog.procedure = previous
    dialog._variables = {
        "code": _Variable(previous.code),
        "name": _Variable("ЛФК, уточнено"),
        "specialist": _Variable(previous.specialist),
        "count": _Variable("2"),
        "duration": _Variable("45"),
        "frequency": _Variable(previous.frequency),
    }

    dialog.apply()

    assert dialog.result is not None
    assert dialog.result.duration_minutes == 45
    assert dialog.result.performed_dates == previous.performed_dates
    assert dialog.result.count_needs_review is True


def test_icf_domain_can_keep_responsible_specialist_blank(tmp_path) -> None:
    previous = IcfDomain(
        code="e310",
        description="Семья и ближайшие родственники",
        specialist=SpecialistRole.FRM,
        initial=IcfQualifier(4, facilitator=True),
        initial_source=tmp_path / "невролог.docx",
        initial_measured_at=datetime(2026, 8, 3, 8, 30),
    )
    dialog = object.__new__(dialogs_module.IcfDomainDialog)
    dialog.domain = previous
    dialog._variables = {
        "code": _Variable(previous.code),
        "description": _Variable(previous.description),
        "role": _Variable(""),
        "initial": _Variable("4+"),
        "final": _Variable(""),
        "note": _Variable(""),
    }

    assert dialog.validate()
    dialog.apply()

    assert dialog.result is not None
    assert dialog.result.specialist is SpecialistRole.OTHER
    assert dialog.result.initial_source == previous.initial_source
    assert dialog.result.initial_measured_at == previous.initial_measured_at


def test_initial_icf_editor_preserves_hidden_repeat_point(tmp_path) -> None:
    previous = IcfDomain(
        code="d450",
        description="Ходьба",
        specialist=SpecialistRole.PHYSICAL_THERAPIST,
        initial=IcfQualifier(3),
        final=IcfQualifier(1),
        initial_source=tmp_path / "ft-primary.docx",
        final_source=tmp_path / "ft-final.docx",
        initial_measured_at=datetime(2026, 8, 3, 10),
        final_measured_at=datetime(2026, 8, 14, 10),
    )
    dialog = object.__new__(dialogs_module.IcfDomainDialog)
    dialog.domain = previous
    dialog.kind = MdrkKind.INITIAL
    dialog._variables = {
        "code": _Variable(previous.code),
        "description": _Variable(previous.description),
        "role": _Variable(previous.specialist.display_name),
        "initial": _Variable("3"),
        "note": _Variable(""),
    }

    assert dialog.validate()
    dialog.apply()

    assert dialog.result is not None
    assert dialog.result.final == IcfQualifier(1)
    assert dialog.result.final_source == previous.final_source
    assert dialog.result.final_measured_at == previous.final_measured_at


def test_initial_ui_hides_repeat_icf_and_intermediate_scale_and_finding_rows(
    tmp_path,
) -> None:
    app = object.__new__(MdrkBuilderApp)
    app._current_kind = MdrkKind.INITIAL
    app.episode = Episode(folder=tmp_path)
    app.episode.initial_meeting_at = datetime(2026, 8, 4, 8)
    app.episode.final_meeting_at = datetime(2026, 8, 15, 8)
    app.episode.icf_domains = [
        IcfDomain(
            "d450",
            "Ходьба",
            SpecialistRole.PHYSICAL_THERAPIST,
            initial=IcfQualifier(3),
            final=IcfQualifier(1),
            initial_source=tmp_path / "ft-primary.docx",
            final_source=tmp_path / "ft-final.docx",
            initial_measured_at=datetime(2026, 8, 3, 10),
            final_measured_at=datetime(2026, 8, 14, 10),
        ),
        IcfDomain(
            "d640",
            "Домашняя работа",
            SpecialistRole.OCCUPATIONAL_THERAPIST,
            initial=IcfQualifier(2),
            initial_source=tmp_path / "ot-follow-up.docx",
            initial_measured_at=datetime(2026, 8, 10, 10),
        ),
    ]
    role = SpecialistRole.PHYSICAL_THERAPIST
    first = ScaleMeasurement(
        "Berg", "20", datetime(2026, 8, 3, 10), role, tmp_path / "ft-primary.docx"
    )
    middle = ScaleMeasurement(
        "Berg", "30", datetime(2026, 8, 8, 10), role, tmp_path / "ft-diary.docx"
    )
    last = ScaleMeasurement(
        "Berg", "40", datetime(2026, 8, 14, 10), role, tmp_path / "ft-final.docx"
    )
    app.episode.findings = [
        SpecialistFinding(role, "первичное", first.measured_at, first.source, [first]),
        SpecialistFinding(role, "дневник", middle.measured_at, middle.source, [middle]),
        SpecialistFinding(role, "итоговое", last.measured_at, last.source, [last]),
    ]
    app.icf_tree = _DataTree()
    app.scale_tree = _DataTree()
    app.finding_tree = _DataTree()
    app._scale_refs = []

    app._refresh_icf()
    app._refresh_scales()
    app._refresh_findings()

    assert app.icf_tree.displaycolumns == (
        "code",
        "description",
        "role",
        "initial",
        "note",
    )
    assert list(app.icf_tree.rows) == ["0"]
    assert [row[2] for row in app.scale_tree.rows.values()] == ["Berg"]
    assert [row[3] for row in app.finding_tree.rows.values()] == ["первичное"]

    app._current_kind = MdrkKind.FINAL
    app._refresh_icf()
    app._refresh_scales()
    app._refresh_findings()

    assert "final" in app.icf_tree.displaycolumns
    assert list(app.icf_tree.rows) == ["0", "1"]
    assert [row[3] for row in app.scale_tree.rows.values()] == ["20", "40"]
    assert [row[3] for row in app.finding_tree.rows.values()] == ["итоговое"]


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


def test_selected_issue_can_be_ignored_after_explicit_confirmation(
    monkeypatch,
    tmp_path,
) -> None:
    issue = ReviewIssue(
        code="arbitrary_non_whitelisted_blocker",
        message="Не набрано 180 минут",
        severity=ReviewSeverity.BLOCKING,
        field="procedures.daily_minutes",
        source=tmp_path / "назначения.docx",
    )
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app._current_kind = MdrkKind.INITIAL
    app._issue_refs = {"0": issue}
    app.issue_tree = _SelectableTree("0")
    app.status_var = _Variable()
    applied: list[MdrkKind] = []
    prompts: list[str] = []

    def apply_form(kind: MdrkKind) -> bool:
        applied.append(kind)
        return True

    app._apply_form = apply_form
    app._refresh_issues = lambda: None
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda _title, message: prompts.append(message) or True,
    )

    app._acknowledge_selected_issue()

    assert applied == [MdrkKind.INITIAL]
    assert is_issue_acknowledged(app.episode, issue, MdrkKind.INITIAL)
    assert prompts
    assert "БЛОКИРУЕТ" in prompts[0]
    assert issue.message in prompts[0]
    assert str(issue.source) in prompts[0]
    assert "останется видимой" in prompts[0]
    assert "игнорируется" in app.status_var.get()


def test_reset_issue_acknowledgements_clears_all_kinds(monkeypatch, tmp_path) -> None:
    initial_issue = ReviewIssue("initial_warning", "Нужна проверка")
    final_issue = ReviewIssue("final_warning", "Нужна проверка в МДРК-2")
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app._current_kind = MdrkKind.INITIAL
    app.status_var = _Variable()
    refreshes: list[bool] = []
    app._refresh_issues = lambda: refreshes.append(True)
    acknowledge_issue(app.episode, initial_issue, MdrkKind.INITIAL)
    acknowledge_issue(app.episode, final_issue, MdrkKind.FINAL)
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *_args: True)

    app._reset_issue_acknowledgements()

    assert not has_issue_acknowledgements(app.episode)
    assert refreshes == [True]
    assert "сброшено" in app.status_var.get()


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


def test_rescan_passes_changed_admission_and_recomputes_default_meetings(
    monkeypatch,
    tmp_path,
) -> None:
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app.episode.identity.medical_record_number = "123/26"
    app.episode.materialized_medical_record_number = "123/26"
    app.episode.admission_datetime = datetime(2026, 8, 9, 12)
    app.episode.materialized_admission_datetime = datetime(2026, 8, 9, 12)
    app.episode.initial_meeting_at = datetime(2026, 8, 10, 8)
    app.episode.final_meeting_at = datetime(2026, 8, 20, 11)
    app._current_kind = MdrkKind.INITIAL
    app._scanning = False
    app._scan_results = queue.Queue()
    app._scan_thread = None
    app._scan_folder = None
    app._entry_variables = {
        "record_number": _Variable("123/26"),
        "admission": _Variable("08.08.2026 12:00"),
        "meeting": _Variable("10.08.2026 08:00"),
    }
    app.folder_var = _Variable(str(tmp_path))
    app.status_var = _Variable()
    app.root = _Root()
    app._invalidate_episode = lambda: setattr(app, "episode", None)
    app._set_folder_field = lambda value: app.folder_var.set(value)
    app._set_scanning = lambda value: setattr(app, "_scanning", value)
    captured: dict[str, object] = {}

    def fake_scan(folder, **kwargs) -> Episode:
        captured["folder"] = folder
        captured.update(kwargs)
        return Episode(folder=folder)

    monkeypatch.setattr(app_module, "scan_patient_folder", fake_scan)
    monkeypatch.setattr(app_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda _title, _message: True,
    )

    app._start_scan()

    assert captured == {
        "folder": tmp_path,
        "medical_record_number_override": "123/26",
        "admission_datetime_override": datetime(2026, 8, 8, 12),
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


def test_russian_and_windows_layout_shortcuts_generate_native_virtual_events(
    monkeypatch,
) -> None:
    widget = _KeyboardWidget()

    result = dialogs_module._dispatch_control_shortcut(
        SimpleNamespace(widget=widget, keysym="Cyrillic_em", keycode=86)
    )

    assert result == "break"
    assert widget.generated_events == ["<<Paste>>"]

    widget.generated_events.clear()
    monkeypatch.setattr(dialogs_module.sys, "platform", "win32")
    result = dialogs_module._dispatch_control_shortcut(
        SimpleNamespace(widget=widget, keysym="??", keycode=88)
    )

    assert result == "break"
    assert widget.generated_events == ["<<Cut>>"]


def test_latin_clipboard_shortcut_is_left_to_native_tk_binding() -> None:
    widget = _KeyboardWidget()

    result = dialogs_module._dispatch_control_shortcut(
        SimpleNamespace(widget=widget, keysym="v", keycode=86)
    )

    assert result is None
    assert widget.generated_events == []


def test_select_all_handles_latin_and_russian_entry_shortcuts() -> None:
    for keysym in ("a", "Cyrillic_ef", "ф"):
        widget = _KeyboardWidget()

        result = dialogs_module._dispatch_control_shortcut(
            SimpleNamespace(widget=widget, keysym=keysym, keycode=65)
        )

        assert result == "break"
        assert widget.selection_range_args == (0, "end")
        assert widget.cursor == "end"


def test_tree_shortcuts_copy_rows_and_select_all() -> None:
    tree = _KeyboardTree()

    assert dialogs_module._dispatch_control_shortcut(
        SimpleNamespace(widget=tree, keysym="c", keycode=67)
    ) == "break"
    assert tree.clipboard == "в\tг"

    assert dialogs_module._dispatch_control_shortcut(
        SimpleNamespace(widget=tree, keysym="Cyrillic_ef", keycode=65)
    ) == "break"
    assert tree.selected == ("0", "1")


def test_delete_key_binding_invokes_collection_delete() -> None:
    callbacks: dict[str, object] = {}
    calls: list[str] = []

    class BindingTree:
        def bind(self, sequence: str, callback) -> None:
            callbacks[sequence] = callback

    MdrkBuilderApp._bind_tree_delete(BindingTree(), lambda: calls.append("deleted"))

    for sequence in ("<Delete>", "<KP_Delete>"):
        assert callbacks[sequence](SimpleNamespace()) == "break"  # type: ignore[operator]
    assert calls == ["deleted", "deleted"]


def test_collection_deletes_remove_all_selected_rows_and_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    app = object.__new__(MdrkBuilderApp)
    app.episode = Episode(folder=tmp_path)
    app.episode.icf_domains = [
        IcfDomain(f"d{index}", f"домен {index}", SpecialistRole.FRM)
        for index in range(3)
    ]
    app.episode.procedures = [
        Procedure(f"процедура {index}", "врач", index)
        for index in range(3)
    ]
    first_scales = [
        ScaleMeasurement("A", "1", None, SpecialistRole.LOGOPEDIST),
        ScaleMeasurement("B", "2", None, SpecialistRole.LOGOPEDIST),
    ]
    second_scales = [
        ScaleMeasurement("C", "3", None, SpecialistRole.NEUROPSYCHOLOGIST)
    ]
    app.episode.findings = [
        SpecialistFinding(SpecialistRole.LOGOPEDIST, scales=first_scales),
        SpecialistFinding(SpecialistRole.NEUROPSYCHOLOGIST, scales=second_scales),
    ]
    app.icf_tree = _SelectableTree(("0", "2"))
    app.procedure_tree = _SelectableTree(("0", "2"))
    app.scale_tree = _SelectableTree(("0", "2"))
    app.finding_tree = _SelectableTree(("1",))
    app._scale_refs = [(0, 0), (0, 1), (1, 0)]
    refreshes: list[str] = []
    app._refresh_icf = lambda: refreshes.append("icf")
    app._refresh_procedures = lambda: refreshes.append("procedures")
    app._refresh_scales = lambda: refreshes.append("scales")
    app._refresh_findings = lambda: refreshes.append("findings")
    app._refresh_issues = lambda: refreshes.append("issues")
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *_args: True)

    app._delete_icf()
    app._delete_procedure()
    app._delete_scale()
    app._delete_finding()

    assert [domain.code for domain in app.episode.icf_domains] == ["d1"]
    assert [procedure.name for procedure in app.episode.procedures] == ["процедура 1"]
    assert [scale.name for scale in app.episode.findings[0].scales] == ["B"]
    assert len(app.episode.findings) == 1
    assert refreshes.count("issues") == 4
    assert {"icf", "procedures", "scales", "findings"}.issubset(refreshes)
