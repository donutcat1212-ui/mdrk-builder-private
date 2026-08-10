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
    leading_lines = [
        cleaned.casefold()
        for paragraph in document.paragraphs[:12]
        for raw_line in paragraph.splitlines()
        if (cleaned := clean_text(raw_line))
    ][:24]
    leading_content = clean_text(" ".join(leading_lines[:8])).casefold()[:1600]
    heading_content = clean_text(" ".join(leading_lines)).casefold()[:4000]
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
    treating_neurologist = bool(
        re.search(
            r"лечащ\w*\s+врач\s*,?\s*врач[- ]невролог\b",
            content,
        )
    )
    treating_frm = bool(
        re.search(
            r"лечащ\w*\s+врач\s*,?\s*(?:врач\s+фрм\b|"
            r"врач\w*\s+физическ\w*\s+и\s+реабилитационн\w*\s+медицин\w*)",
            content,
        )
    )
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
    heading_role_patterns = (
        (SpecialistRole.PATHOPSYCHOLOGIST, r"\bпатопсихолог\w*"),
        (SpecialistRole.NEUROPSYCHOLOGIST, r"\bнейропсихолог\w*"),
        (SpecialistRole.LOGOPEDIST, r"\bлогопед\w*"),
        (SpecialistRole.OCCUPATIONAL_THERAPIST, r"\b(?:эрготерап|эргореабил)\w*"),
        (
            SpecialistRole.PHYSICAL_THERAPIST,
            r"\bспециалист\w*\s+по\s+физическ\w*\s+реабилитац\w*",
        ),
    )
    specialist_heading_pattern = re.compile(
        r"\b(?:первичн|повторн|заключительн|итогов)\w*\b.{0,100}"
        r"\b(?:осмотр|консультаци\w*|обследовани\w*)\b",
        re.IGNORECASE,
    )
    heading_role = next(
        (
            candidate
            for line in leading_lines
            if specialist_heading_pattern.search(line)
            for candidate, pattern in heading_role_patterns
            if re.search(pattern, line)
        ),
        None,
    )
    physician_template_heading = "лечащим врачом" in leading_content
    physician_override_allowed = physician_template_heading or scored_role in physician_or_unknown
    if heading_role is not None:
        role = heading_role
    elif treating_neurologist and physician_override_allowed:
        # A physician template can mention every MDRK participant in its plan.
        # The treating doctor's own job title is stronger evidence than those
        # incidental specialist mentions.
        role = SpecialistRole.NEUROLOGIST
    elif treating_frm and physician_override_allowed:
        role = SpecialistRole.FRM
    else:
        role = (
            SpecialistRole.FRM
            if explicit_frm and scored_role in physician_or_unknown
            else scored_role
        )
    score = max(
        role_scores[role],
        20 if explicit_frm or heading_role is role else 0,
    )
    if score == 0:
        role = SpecialistRole.OTHER

    heading_patterns = (
        (
            "initial",
            r"\bпервичн\w*\s+(?:осмотр|консультаци\w*|обследовани\w*)",
        ),
        ("final", r"\b(?:выписной|заключительный)\b|курс реабилитации завершен"),
        ("follow_up", r"\bповторн\w*\s+(?:осмотр|консультаци\w*)"),
    )
    heading_matches = [
        (match.start(), kind)
        for kind, pattern in heading_patterns
        if (match := re.search(pattern, heading_content)) is not None
    ]
    if heading_matches:
        document_type = min(heading_matches)[1]
    elif any(token in content for token in ("выписной", "заключительный", "курс реабилитации завершен")):
        document_type = "final"
    elif any(token in content for token in ("повторный осмотр", "повторная консультация")):
        document_type = "follow_up"
    elif re.search(r"\bзаключение\b", content):
        document_type = "consultation"
    else:
        document_type = "unknown"
    confidence = min(1.0, 0.45 + score * 0.08)
    return DocumentClassification(role, document_type, confidence=confidence)
