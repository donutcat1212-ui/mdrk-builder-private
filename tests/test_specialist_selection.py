from pathlib import Path
from types import SimpleNamespace

from mdrk_builder.domain import Episode, SpecialistFinding, SpecialistRole
from mdrk_builder.ui.app import MdrkBuilderApp


def make_app():
    app = MdrkBuilderApp.__new__(MdrkBuilderApp)
    first = SpecialistFinding(SpecialistRole.NEUROLOGIST, conclusion="Первое заключение")
    second = SpecialistFinding(SpecialistRole.LOGOPEDIST, conclusion="Второе заключение")
    app.episode = Episode(folder=Path("."))
    app.episode.findings = [first, second]
    app._loading_specialist = False
    app._displayed_specialist_finding = first
    app.finding_tree = SimpleNamespace(selection=lambda: ("1",))
    app.specialist_conclusion = SimpleNamespace(get=lambda *args: "Ручная правка первого")
    app._refresh_issues = lambda: None
    app._refresh_specialist_detail = lambda: None
    return app, first, second


def test_switch_specialist_commits_text_to_displayed_owner():
    app, first, second = make_app()
    app._on_specialist_selected()
    assert first.conclusion == "Ручная правка первого"
    assert second.conclusion == "Второе заключение"
    assert "findings" in app._manual_collections


def test_stale_editor_cannot_change_replacement_episode():
    app, first, second = make_app()
    app.episode.findings = [second]
    app._commit_specialist_conclusion()
    assert first.conclusion == "Первое заключение"
    assert second.conclusion == "Второе заключение"
