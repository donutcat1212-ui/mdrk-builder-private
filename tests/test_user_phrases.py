from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk

import pytest

from mdrk_builder.infrastructure import phrase_store
from mdrk_builder.infrastructure.phrase_store import PhraseStore, PhraseStorageError
from mdrk_builder.ui.dialogs import _show_edit_context_menu
from mdrk_builder.ui.formatted_text import FormattedText
from mdrk_builder.ui.quick_phrases import PhraseDialog, QuickPhrases
from test_source_access import root


def test_new_phrase_file_is_empty_and_portable_across_program_folders(tmp_path):
    store = PhraseStore(tmp_path / "user_phrases.json")
    assert store.phrases("conclusion") == ()
    assert not store.path.exists()
    text = "Своя фраза врача.\nВторая строка: ё, №, + и **выделение**."
    store.add("conclusion", text)
    store.add("laboratory_results", "Другая фраза")
    store.add("conclusion", text)
    new_version = tmp_path / "new-version"
    new_version.mkdir()
    copied = shutil.copyfile(store.path, new_version / store.path.name)
    restored = PhraseStore(Path(copied))
    assert restored.phrases("conclusion") == (text,)
    restored.delete("conclusion", text)
    assert PhraseStore(restored.path).phrases("conclusion") == ()
    assert restored.phrases("laboratory_results") == ("Другая фраза",)
    assert store.phrases("conclusion") == (text,)


def test_version_one_contract_preserves_unknown_fields_and_metadata(tmp_path):
    path = tmp_path / "user_phrases.json"
    # This fixture is the stable v1 reader/writer compatibility contract.
    fixture = {"schema_version": 1, "fields": {"conclusion": ["Сохранено ранее"],
               "future_field": ["Фраза неизвестного поля"]}, "future_metadata": {"value": 42}}
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8-sig")
    store = PhraseStore(path)
    assert store.phrases("conclusion") == ("Сохранено ранее",)
    store.add("conclusion", "Добавлено сейчас")
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == 1
    assert saved["future_metadata"] == fixture["future_metadata"]
    assert saved["fields"]["future_field"] == fixture["fields"]["future_field"]
    assert saved["fields"]["conclusion"] == ["Сохранено ранее", "Добавлено сейчас"]


@pytest.mark.parametrize("payload", [b"broken JSON", b'{"schema_version": 2, "fields": {}}',
                                    b'{"schema_version": 1, "fields": {"conclusion": null}}'])
def test_invalid_or_newer_file_is_never_overwritten(tmp_path, payload):
    store = PhraseStore(tmp_path / "user_phrases.json")
    store.path.write_bytes(payload)
    with pytest.raises(PhraseStorageError):
        store.add("conclusion", "Новая фраза")
    with pytest.raises(PhraseStorageError):
        store.delete("conclusion", "Новая фраза")
    assert store.path.read_bytes() == payload


def test_failed_replacement_keeps_saved_phrases_and_releases_lock(tmp_path, monkeypatch):
    store = PhraseStore(tmp_path / "user_phrases.json")
    store.add("conclusion", "Уже сохранено")
    original = store.path.read_bytes()
    def fail(*_args):
        raise PermissionError("test: file is read-only")
    with monkeypatch.context() as patch:
        patch.setattr(phrase_store.os, "replace", fail)
        with pytest.raises(PhraseStorageError):
            store.add("conclusion", "Не сохранилось")
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob(".user_phrases-*.tmp"))
    store.add("conclusion", "Сохранилось после повторной попытки")
    assert len(store.phrases("conclusion")) == 2


def test_shared_phrase_file_does_not_lose_parallel_changes(tmp_path):
    path = tmp_path / "user_phrases.json"
    texts = [f"Фраза {index}" for index in range(16)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda text: PhraseStore(path).add("conclusion", text), texts))
    assert set(PhraseStore(path).phrases("conclusion")) == set(texts)


def test_phrase_file_is_next_to_exe_not_the_patient_or_unpack_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(phrase_store.sys, "frozen", True, raising=False)
    monkeypatch.setattr(phrase_store.sys, "executable", str(tmp_path / "program" / "MDRK_Builder.exe"))
    monkeypatch.setattr(phrase_store.sys, "_MEIPASS", str(tmp_path / "unpacked"), raising=False)
    monkeypatch.chdir(tmp_path)
    assert phrase_store.user_phrases_path() == tmp_path / "program" / "user_phrases.json"


def test_phrase_menu_add_insert_delete_and_refresh_in_another_editor(root, tmp_path, monkeypatch):
    store = PhraseStore(tmp_path / "user_phrases.json")
    widget = FormattedText(root, undo=True)
    menu = QuickPhrases(root, widget, "conclusion", store=store)
    another = QuickPhrases(root, FormattedText(root), "conclusion", store=store)
    menu.refresh()
    assert menu.menu.index("end") == 0
    assert menu.menu.entrycget(0, "label") == "+ Добавить"
    assert widget.get("1.0", "end-1c") == ""
    text = "Первая строка\nВторая строка"
    monkeypatch.setattr("mdrk_builder.ui.quick_phrases.PhraseDialog",
                        lambda parent, storage, field: storage.add(field, text))
    menu.menu.invoke(0)
    assert store.phrases("conclusion") == (text,)
    assert widget.get("1.0", "end-1c") == ""
    menu.menu.invoke(0)
    assert widget.get("1.0", "end-1c") == text
    widget.edit_undo()
    assert widget.get("1.0", "end-1c") == ""
    another.refresh()
    assert another.menu.entrycget(0, "label") == "Первая строка Вторая строка"
    menu.delete_menu.invoke(0)
    another.refresh()
    assert another.menu.index("end") == 0
    assert PhraseStore(store.path).phrases("conclusion") == ()


def test_add_dialog_keeps_text_after_save_error_then_saves_multiline_text(root, tmp_path, monkeypatch):
    store = PhraseStore(tmp_path / "user_phrases.json")
    monkeypatch.setattr(PhraseDialog, "wait_window", lambda *_: None)
    monkeypatch.setattr(PhraseDialog, "wait_visibility", lambda *_: None)
    errors = []
    monkeypatch.setattr("mdrk_builder.ui.quick_phrases.messagebox.showerror", lambda *args, **kw: errors.append(args))
    dialog = PhraseDialog(root, store, "conclusion")
    assert not dialog.bind("<Return>")
    assert dialog.bind("<Control-Return>")
    dialog.ok()
    assert dialog.winfo_exists() and errors
    dialog.text.insert("1.0", "Своя фраза\nНесколько строк")
    def fail(*_):
        raise PhraseStorageError("test: no write permission")
    with monkeypatch.context() as patch:
        patch.setattr(store, "add", fail)
        dialog.ok()
        assert dialog.winfo_exists()
        assert dialog.text.get("1.0", "end-1c") == "Своя фраза\nНесколько строк"
    dialog.ok()
    assert not dialog.winfo_exists()
    assert store.phrases("conclusion") == ("Своя фраза\nНесколько строк",)


def test_bold_is_in_right_click_menu_and_toolbar_button_is_absent(root, monkeypatch):
    widget = FormattedText(root, undo=True)
    widget.insert("1.0", "Выделяемая фраза")
    widget.tag_add("sel", "1.0", "1.10")
    captured = []
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda menu, *args: captured.append(menu))
    event = SimpleNamespace(widget=widget, x_root=0, y_root=0)
    assert _show_edit_context_menu(event) == "break"
    menu = captured[-1]
    index = next(i for i in range(menu.index("end") + 1)
                 if menu.type(i) == "command" and menu.entrycget(i, "label") == "Полужирный")
    menu.invoke(index)
    assert widget.get("1.0", "end-1c").startswith("**Выделяемая**")
    menu.invoke(index)
    assert not widget.tag_ranges("bold")
    assert not any(isinstance(child, ttk.Button) for child in widget.frame.winfo_children())
    widget.configure(state="disabled")
    _show_edit_context_menu(event)
    assert captured[-1].entrycget(index, "state") == "disabled"


@pytest.mark.parametrize("action", ["phrase", "bold"])
def test_menu_edits_are_recorded_as_manual_mdrk_changes(root, tmp_path, action):
    from mdrk_builder.domain import MdrkKind
    from mdrk_builder.ui.app import MdrkBuilderApp
    from mdrk_builder.ui.quick_phrases import insert_phrase
    from test_docx_writer import _representative_episode
    app = MdrkBuilderApp(root)
    app.episode = _representative_episode(tmp_path)
    app._populate_from_episode()
    widget = app._text_fields["clinical_diagnosis"]
    app._dirty_section_fields[MdrkKind.INITIAL].clear()
    if action == "phrase":
        insert_phrase(widget, "ФРАЗА ВРАЧА ")
    else:
        widget.tag_add("sel", "1.0", "1.5")
        widget.toggle_bold()
    assert "clinical_diagnosis" in app._dirty_section_fields[MdrkKind.INITIAL]
    assert app._apply_form()
    assert app.episode.initial_sections.clinical_diagnosis == widget.get("1.0", "end-1c").strip()
