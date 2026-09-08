"""Keep synthetic autosave data inside the current pytest workspace."""
import pytest


@pytest.fixture(autouse=True)
def isolated_workspace_drafts(tmp_path, monkeypatch):
    from mdrk_builder.infrastructure import draft_store
    monkeypatch.setattr(draft_store, "_workspace_draft_directory", lambda: tmp_path / "app-data")
