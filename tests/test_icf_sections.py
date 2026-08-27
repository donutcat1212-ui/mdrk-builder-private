from mdrk_builder.domain import (
    IcfDomain,
    IcfSection,
    SpecialistRole,
    infer_icf_section,
    move_icf_domain,
)


def test_icf_code_is_assigned_to_expected_section() -> None:
    assert infer_icf_section("b730") is IcfSection.BODY_FUNCTIONS
    assert infer_icf_section(" S 110 ") is IcfSection.BODY_STRUCTURES
    assert infer_icf_section("d450") is IcfSection.ACTIVITIES_PARTICIPATION
    assert infer_icf_section("е120") is IcfSection.ENVIRONMENTAL_FACTORS
    assert infer_icf_section("Pf") is IcfSection.PERSONAL_FACTORS


def test_unknown_icf_code_falls_back_to_personal_factors() -> None:
    assert infer_icf_section("") is IcfSection.PERSONAL_FACTORS
    assert infer_icf_section("x999") is IcfSection.PERSONAL_FACTORS


def test_manual_section_override_wins_over_code_classification() -> None:
    domain = IcfDomain("b730", "Сила мышц", SpecialistRole.FRM)
    assert domain.section is IcfSection.BODY_FUNCTIONS

    domain.section_override = IcfSection.ACTIVITIES_PARTICIPATION

    assert domain.section is IcfSection.ACTIVITIES_PARTICIPATION


def test_move_icf_domain_changes_section_and_order() -> None:
    domains = [
        IcfDomain("b730", "Сила мышц", SpecialistRole.FRM),
        IcfDomain("d450", "Ходьба", SpecialistRole.FRM),
        IcfDomain("e120", "Технические средства", SpecialistRole.FRM),
    ]

    new_index = move_icf_domain(
        domains,
        0,
        IcfSection.ENVIRONMENTAL_FACTORS,
        before_index=2,
    )

    assert new_index == 1
    assert [domain.code for domain in domains] == ["d450", "b730", "e120"]
    assert domains[new_index].section is IcfSection.ENVIRONMENTAL_FACTORS


def test_move_icf_domain_to_section_end() -> None:
    domains = [
        IcfDomain("b730", "Сила мышц", SpecialistRole.FRM),
        IcfDomain("x1", "Привычка", SpecialistRole.OTHER),
        IcfDomain("d450", "Ходьба", SpecialistRole.FRM),
    ]

    new_index = move_icf_domain(domains, 2, IcfSection.PERSONAL_FACTORS)

    assert new_index == 2
    assert [domain.code for domain in domains] == ["b730", "x1", "d450"]
    assert domains[new_index].section is IcfSection.PERSONAL_FACTORS
