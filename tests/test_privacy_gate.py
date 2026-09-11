from pathlib import Path

from tools.privacy_gate import (
    audit_docx,
    audit_release_candidate,
    audit_source_tree,
)


def test_canonical_template_has_no_hidden_privacy_surfaces() -> None:
    template = Path("src/mdrk_builder/resources/canonical_mdrk_template.docx")

    assert audit_docx(template) == []


def test_discharge_template_has_only_approved_static_branding() -> None:
    template = Path("src/mdrk_builder/resources/discharge_summary_template.docx")

    assert audit_docx(template) == []


def test_source_gate_rejects_identity_and_local_user_path(tmp_path) -> None:
    source = tmp_path / "fixture.py"
    local_path = "/" + "Users" + "/account/Documents/source.docx"
    identity = "ФИО пациента: " + "АЛЬФА БЕТА ГАММА".title()
    source.write_text(f'{local_path}\n{identity}\n', encoding="utf-8")

    reasons = {finding.reason for finding in audit_source_tree(tmp_path)}

    assert "локальный абсолютный путь пользователя" in reasons
    assert "реалистичное ФИО после идентифицирующей метки" in reasons


def test_source_gate_allows_only_fixed_department_head_name(tmp_path) -> None:
    allowed = tmp_path / "allowed.py"
    allowed.write_text('DEPARTMENT_HEAD = "Поляев Б.Б."\n', encoding="utf-8")

    assert audit_source_tree(tmp_path) == []

    blocked = tmp_path / "blocked.py"
    blocked_name = "Ива" + "нов И.И."
    blocked.write_text(f'SPECIALIST = "{blocked_name}"\n', encoding="utf-8")

    assert any(
        finding.reason == "реалистичная фамилия с инициалами"
        for finding in audit_source_tree(tmp_path)
    )


def test_release_gate_rejects_patient_source_format(tmp_path) -> None:
    candidate = tmp_path / "MDRK_Builder_1.0.0_Internal"
    candidate.mkdir()
    (candidate / "MDRK_Builder.exe").write_bytes(b"MZ")
    (candidate / "source.docx").write_bytes(b"not a release artifact")

    findings = audit_release_candidate(candidate)

    assert any("patient/source формат" in finding.reason for finding in findings)


def test_release_gate_rejects_populated_feedback_in_new_delivery(tmp_path) -> None:
    candidate = tmp_path / "MDRK_Builder_1.0.0_Internal"
    candidate.mkdir()
    feedback = "ФИО пациента: АЛЬФА БЕТА ГАММА"
    (candidate / "issues.txt").write_text(feedback, encoding="utf-8")

    assert any("журнал обратной связи" in finding.reason for finding in audit_release_candidate(candidate))


def test_release_gate_allows_empty_feedback(tmp_path):
    candidate = tmp_path / "delivery"
    candidate.mkdir()
    (candidate / "issues.txt").write_bytes(b"\xef\xbb\xbf")
    assert audit_release_candidate(candidate) == []


def test_user_phrase_file_is_excluded_even_when_empty(tmp_path):
    (tmp_path / "user_phrases.json").write_text('{"schema_version": 1, "fields": {}}')
    assert any("фраз" in finding.reason for finding in audit_source_tree(tmp_path))
    assert any("фраз" in finding.reason for finding in audit_release_candidate(tmp_path))


def test_scale_eponym_exception_does_not_allow_a_patient_name(tmp_path):
    from tools.privacy_gate import ALLOWED_CLINICAL_TERMS
    term = ALLOWED_CLINICAL_TERMS[0]
    path = tmp_path / 'scale.py'
    path.write_text(term, encoding='utf-8')
    assert audit_source_tree(tmp_path) == []
    path.write_text('Пациент: ' + term.removeprefix('Шкала '), encoding='utf-8')
    assert any(f.reason == 'реалистичная фамилия с инициалами' for f in audit_source_tree(tmp_path))
