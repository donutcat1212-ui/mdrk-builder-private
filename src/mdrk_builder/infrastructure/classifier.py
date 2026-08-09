from __future__ import annotations

import re
from dataclasses import dataclass

from mdrk_builder.domain import SpecialistRole
from mdrk_builder.infrastructure.ooxml_reader import ParsedDocument, clean_text


@dataclass(frozen=True, slots=True)
class DocumentClassification:
    role: SpecialistRole
    document_type: str
    is_mdrk: bool = False
    confidence: float = 0.5


def _haystack(document: ParsedDocument) -> tuple[str, str]:
    path_text = clean_text(str(document.source_path)).casefold()
    content = clean_text(document.text[:30000]).casefold()
    return path_text, content


def classify_document(document: ParsedDocument) -> DocumentClassification:
    path_text, content = _haystack(document)
    if "консилиум мультидисциплинарной реабилитационной команды" in content:
        return DocumentClassification(SpecialistRole.OTHER, "mdrk", is_mdrk=True, confidence=1.0)

    if "консилиум" in content and any(
        token in f"{path_text} {content[:3000]}" for token in ("гастростом", "пэг", "peg")
    ):
        return DocumentClassification(SpecialistRole.OTHER, "other_consilium", confidence=0.99)

    assignment_table = any(
        table.rows
        and table.rows[0].logical_cols >= 10
        and table.rows[0].cells
        and "назначения" in table.rows[0].cells[0].text.casefold()
        for table in document.tables
    )
    if "лист назначений консилиума" in content or assignment_table:
        return DocumentClassification(SpecialistRole.OTHER, "assignment_sheet", confidence=0.98)

    if any(
        token in path_text
        for token in ("титул", "оборотная сторона раздела", "обложка")
    ) or "оборотная сторона раздела \"лист назначений и их выполнение\"" in content:
        return DocumentClassification(SpecialistRole.OTHER, "administrative", confidence=0.99)

    role_scores: dict[SpecialistRole, int] = {role: 0 for role in SpecialistRole}
    patterns: dict[SpecialistRole, tuple[tuple[str, ...], tuple[str, ...]]] = {
        SpecialistRole.PATHOPSYCHOLOGIST: (("патопсих",), ("патопсих", "патопсихолог")),
        SpecialistRole.NEUROPSYCHOLOGIST: (("нейропсих",), ("нейропсих", "нейропсихолог")),
        SpecialistRole.LOGOPEDIST: (("логоп",), ("логопед", "логопедическ")),
        SpecialistRole.OCCUPATIONAL_THERAPIST: (("эрго",), ("эргореабил", "эрготерап")),
        SpecialistRole.PHYSICAL_THERAPIST: (
            ("/фт/", "\\фт\\", "/лфк/", "\\лфк\\", "осмотр фт", "осмотр лфк"),
            ("специалист по физической реабилитации", "физический терапевт"),
        ),
        SpecialistRole.FRM: (
            ("фрм",),
            ("врач физической и реабилитационной медицины", "врач фрм"),
        ),
        SpecialistRole.NEUROLOGIST: (("невролог",), ("осмотр невролога", "врач-невролог")),
    }
    padded_path = "/" + path_text.replace("\\", "/")
    for role, (path_patterns, content_patterns) in patterns.items():
        role_scores[role] += 25 * sum(pattern in padded_path for pattern in path_patterns)
        role_scores[role] += 5 * sum(pattern in content for pattern in content_patterns)

    scored_role = max(role_scores, key=role_scores.get)
    explicit_frm = any(
        token in content
        for token in (
            "врач физической и реабилитационной медицины",
            "врач фрм",
        )
    )
    physician_or_unknown = {
        SpecialistRole.FRM,
        SpecialistRole.NEUROLOGIST,
        SpecialistRole.OTHER,
    }
    role = (
        SpecialistRole.FRM
        if explicit_frm and scored_role in physician_or_unknown
        else scored_role
    )
    score = max(role_scores[role], 20 if explicit_frm else 0)
    if score == 0:
        role = SpecialistRole.OTHER

    if any(token in content for token in ("выписной", "заключительный", "курс реабилитации завершен")):
        document_type = "final"
    elif any(token in content for token in ("повторный осмотр", "динамика", "повторная консультация")):
        document_type = "follow_up"
    elif any(token in content for token in ("первичный осмотр", "первичная консультация")):
        document_type = "initial"
    elif re.search(r"\bзаключение\b", content):
        document_type = "consultation"
    else:
        document_type = "unknown"
    confidence = min(1.0, 0.45 + score * 0.08)
    return DocumentClassification(role, document_type, confidence=confidence)
