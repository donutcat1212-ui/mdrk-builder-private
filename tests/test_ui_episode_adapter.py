from datetime import date, datetime

import pytest

from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    IcfQualifier,
    MdrkKind,
    Procedure,
    SourceDocument,
    SpecialistFinding,
    SpecialistRole,
)
from mdrk_builder.ui.episode_adapter import (
    apply_episode_form_data,
    episode_signatory_roles,
    format_date,
    format_datetime,
    parse_episode_folder,
    parse_episode_form_data,
    parse_optional_date,
    parse_optional_datetime,
    parse_optional_meeting_datetime,
    parse_optional_nonnegative_int,
    parse_qualifier,
)


def test_date_and_datetime_round_trip() -> None:
    assert format_date(parse_optional_date("10.08.2026")) == "10.08.2026"
    assert format_datetime(parse_optional_datetime("10.08.2026 08:00")) == "10.08.2026 08:00"
    assert parse_optional_date("2026-08-10") == date(2026, 8, 10)
    assert parse_optional_datetime("2026-08-10") == datetime(2026, 8, 10)


def test_optional_nonnegative_integer_validation() -> None:
    assert parse_optional_nonnegative_int("", "Койко-дни") is None
    assert parse_optional_nonnegative_int("0", "Койко-дни") == 0
    with pytest.raises(ValueError, match="не может быть отрицательным"):
        parse_optional_nonnegative_int("-1", "Койко-дни")


def test_meeting_datetime_requires_explicit_time() -> None:
    assert parse_optional_meeting_datetime("10.08.2026 08:00") == datetime(
        2026, 8, 10, 8, 0
    )
    with pytest.raises(ValueError, match="Время заседания"):
        parse_optional_meeting_datetime("10.08.2026")


def test_icf_qualifier_supports_barrier_and_facilitator() -> None:
    assert parse_qualifier("3") == IcfQualifier(3, facilitator=False)
    assert parse_qualifier("4+") == IcfQualifier(4, facilitator=True)
    assert parse_qualifier("") is None
    with pytest.raises(ValueError, match="0–4"):
        parse_qualifier("5")


def test_form_is_parsed_before_selected_snapshot_is_mutated(tmp_path) -> None:
    episode = Episode(folder=tmp_path)
    episode.initial_sections.clinical_diagnosis = "исходный"
    episode.sections.clinical_diagnosis = "итоговый"
    entries = {
        "full_name": "ПАЦИЕНТ_ТЕСТ",
        "record_number": "123",
        "birth_date": "01.01.2000",
        "sex": "мужской",
        "admission": "09.08.2026 12:00",
        "meeting": "10.08.2026 08:00",
        "department": "ОМР",
        "stage": "2 этап",
        "duration": "14",
    }
    form = parse_episode_form_data(entries, {"clinical_diagnosis": "исправленный"})

    apply_episode_form_data(episode, MdrkKind.INITIAL, form)

    assert episode.initial_sections.clinical_diagnosis == "исправленный"
    assert episode.sections.clinical_diagnosis == "итоговый"
    assert episode.initial_meeting_at == datetime(2026, 8, 10, 8, 0)

    invalid_entries = {**entries, "meeting": "10.08.2026"}
    with pytest.raises(ValueError, match="Время заседания"):
        parse_episode_form_data(invalid_entries, {"clinical_diagnosis": "не коммитить"})
    assert episode.initial_sections.clinical_diagnosis == "исправленный"


def test_signatory_roles_are_snapshot_participants_not_procedure_owners(tmp_path) -> None:
    episode = Episode(folder=tmp_path)
    episode.initial_meeting_at = datetime(2026, 6, 6, 8)
    episode.final_meeting_at = datetime(2026, 6, 20, 11)
    episode.sources.extend(
        (
            SourceDocument(
                tmp_path / "doctor.docx",
                role=SpecialistRole.NEUROLOGIST,
                clinical_datetime=datetime(2026, 6, 5, 13),
            ),
            SourceDocument(
                tmp_path / "late-ft.docx",
                role=SpecialistRole.PHYSICAL_THERAPIST,
                clinical_datetime=datetime(2026, 6, 19, 14),
            ),
            SourceDocument(tmp_path / "admin.docx", role=SpecialistRole.OTHER),
        )
    )
    episode.findings.append(
        SpecialistFinding(
            role=SpecialistRole.LOGOPEDIST,
            source_datetime=datetime(2026, 6, 5, 14),
        )
    )
    episode.icf_domains.append(
        IcfDomain(
            "d510",
            "Мытьё",
            SpecialistRole.OCCUPATIONAL_THERAPIST,
            final=IcfQualifier(1),
            final_source=tmp_path / "late-ot.docx",
        )
    )
    episode.procedures.append(Procedure("Ходьба", "ФТ", 5))

    assert set(episode_signatory_roles(episode, MdrkKind.INITIAL)) == {
        SpecialistRole.NEUROLOGIST,
        SpecialistRole.LOGOPEDIST,
    }
    final_roles = set(episode_signatory_roles(episode, MdrkKind.FINAL))
    assert SpecialistRole.PHYSICAL_THERAPIST in final_roles
    assert SpecialistRole.OCCUPATIONAL_THERAPIST in final_roles


def test_episode_folder_is_canonicalized(tmp_path) -> None:
    assert parse_episode_folder(str(tmp_path / ".." / tmp_path.name)) == tmp_path.resolve()
    with pytest.raises(ValueError, match="папку эпизода"):
        parse_episode_folder("  ")
