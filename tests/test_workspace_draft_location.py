from copy import deepcopy
from unittest.mock import Mock

import pytest

from mdrk_builder.application.workspace import WorkspaceDraft, MdrkWorkspaceState
from mdrk_builder.domain import Episode, MdrkKind
from mdrk_builder.infrastructure import draft_store
from mdrk_builder.infrastructure.draft_store import _workspace_draft_directory
from mdrk_builder.ui.workspace_state import WorkspacePersistence


def _state(folder):
    return WorkspaceDraft(MdrkKind.INITIAL, "mdrk1", MdrkWorkspaceState(Episode(folder)))


def test_windows_drafts_use_local_appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_store.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert _workspace_draft_directory() == tmp_path / "MDRK Builder" / "drafts"


def test_manual_and_automatic_saves_leave_episode_folder_unchanged(tmp_path):
    folder = tmp_path / "episode"
    folder.mkdir()
    source = folder / "clinical-source.docx"
    source.write_bytes(b"immutable source")
    root = Mock()
    state = _state(folder)
    persistence = WorkspacePersistence(root, capture=lambda: state, restore=Mock(),
        folder=lambda: folder, busy=lambda: False, status=Mock())
    assert persistence.save(explicit=True)
    state.mdrk.entries["full_name"] = "SYNTHETIC EDIT"
    persistence.autosave()
    assert list(folder.iterdir()) == [source]
    assert source.read_bytes() == b"immutable source"
    saved = draft_store.workspace_draft_path(folder)
    assert not saved.is_relative_to(folder)
    assert draft_store.load_draft(saved) == state
    root.after.assert_called_once()


def test_distinct_same_named_episode_folders_have_separate_drafts(tmp_path):
    first = tmp_path / "one" / "episode"
    second = tmp_path / "two" / "episode"
    assert draft_store.workspace_draft_path(first) != draft_store.workspace_draft_path(second)
    assert draft_store.workspace_draft_path(first / ".." / "episode") == draft_store.workspace_draft_path(first)


def test_legacy_draft_moves_without_losing_edits_or_removing_other_json(tmp_path):
    folder = tmp_path / "episode"
    folder.mkdir()
    state = _state(folder)
    state.mdrk.entries["meeting"] = "12."
    legacy = folder / ".mdrk draft.json"
    draft_store.save_draft(legacy, state)
    unrelated = folder / "source.json"
    unrelated.write_text("keep", encoding="utf-8")
    path = draft_store.import_legacy_workspace_draft(folder)
    assert draft_store.load_draft(path) == state
    assert not legacy.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert draft_store.import_legacy_workspace_draft(folder) == path


def test_failed_migration_preserves_original_draft(tmp_path, monkeypatch):
    folder = tmp_path / "episode"
    folder.mkdir()
    legacy = folder / ".mdrk draft.json"
    state = _state(folder)
    draft_store.save_draft(legacy, state)
    monkeypatch.setattr(draft_store, "save_draft", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        draft_store.import_legacy_workspace_draft(folder)
    assert draft_store.load_draft(legacy) == state


def test_existing_local_draft_is_not_overwritten_by_old_folder_draft(tmp_path):
    folder = tmp_path / "episode"
    folder.mkdir()
    old = _state(folder)
    new = deepcopy(old)
    new.mdrk.entries["full_name"] = "LATEST EDIT"
    draft_store.save_draft(folder / ".mdrk draft.json", old)
    path = draft_store.workspace_draft_path(folder)
    draft_store.save_draft(path, new)
    assert draft_store.import_legacy_workspace_draft(folder) == path
    assert draft_store.load_draft(path) == new
