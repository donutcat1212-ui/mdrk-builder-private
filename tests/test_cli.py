from datetime import datetime
from pathlib import Path

from mdrk_builder import cli
from mdrk_builder.domain import Episode, SourceDocument, SpecialistRole


def _valid_episode(folder: Path) -> Episode:
    episode = Episode(folder=folder)
    episode.identity.full_name = "Тестов Тест Тестович"
    episode.identity.medical_record_number = "123"
    episode.admission_datetime = datetime(2026, 8, 9, 12, 0)
    episode.initial_meeting_at = datetime(2026, 8, 10, 8, 0)
    episode.initial_sections.clinical_diagnosis = "Диагноз"
    episode.sections.clinical_diagnosis = "Диагноз"
    episode.sources.append(
        SourceDocument(folder / "doctor.docx", role=SpecialistRole.NEUROLOGIST)
    )
    return episode


def test_existing_output_is_refused_before_scan(monkeypatch, tmp_path, capsys) -> None:
    output = tmp_path / "existing.docx"
    output.write_bytes(b"do not replace")

    def unexpected_scan(_folder: Path) -> Episode:
        raise AssertionError("scanner must not run")

    monkeypatch.setattr(cli, "scan_patient_folder", unexpected_scan)

    assert cli.main([str(tmp_path), "--output", str(output)]) == 1
    assert output.read_bytes() == b"do not replace"
    assert "--force" in capsys.readouterr().err


def test_force_allows_existing_output(monkeypatch, tmp_path) -> None:
    output = tmp_path / "existing.docx"
    output.write_bytes(b"old")
    episode = _valid_episode(tmp_path)
    calls: list[Path] = []

    monkeypatch.setattr(cli, "scan_patient_folder", lambda _folder: episode)

    def fake_write(_episode, _kind, output_path: Path) -> Path:
        calls.append(output_path)
        return output_path

    monkeypatch.setattr(cli, "write_mdrk_docx", fake_write)

    assert cli.main([str(tmp_path), "--output", str(output), "--force"]) == 0
    assert calls == [output]


def test_cli_meeting_rejects_date_without_time(monkeypatch, tmp_path, capsys) -> None:
    scanner_called = False

    def unexpected_scan(_folder: Path, **_kwargs) -> Episode:
        nonlocal scanner_called
        scanner_called = True
        return _valid_episode(tmp_path)

    monkeypatch.setattr(cli, "scan_patient_folder", unexpected_scan)

    assert cli.main([str(tmp_path), "--meeting", "10.08.2026"]) == 1
    assert not scanner_called
    assert "Время заседания" in capsys.readouterr().err


def test_cli_passes_meeting_override_into_scan(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_scan(folder: Path, **kwargs) -> Episode:
        captured["folder"] = folder
        captured.update(kwargs)
        return _valid_episode(tmp_path)

    monkeypatch.setattr(cli, "scan_patient_folder", fake_scan)

    assert cli.main(
        [str(tmp_path), "--kind", "initial", "--meeting", "10.08.2026 09:15"]
    ) == 0
    assert captured == {
        "folder": tmp_path,
        "initial_meeting_at": datetime(2026, 8, 10, 9, 15),
    }
