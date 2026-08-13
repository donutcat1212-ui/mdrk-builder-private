from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from mdrk_builder.application.extractors import (
    extract_admission_datetime,
    extract_clinical_datetime,
    extract_mdrk_document_datetime,
    extract_patient_identity,
    parse_first_datetime,
)
from mdrk_builder.application.scanner import discover_source_files
from mdrk_builder.domain import (
    ReverseSheetDraft,
    ReverseSheetRow,
    ReviewIssue,
    ReviewSeverity,
    SpecialistRole,
)
from mdrk_builder.infrastructure.classifier import DocumentClassification, classify_document
from mdrk_builder.infrastructure.converter import ConversionError, DocumentNormalizer
from mdrk_builder.infrastructure.ooxml_reader import ParsedDocument, clean_text, read_docx


ROLE_INTERVENTIONS = {
    SpecialistRole.FRM: "Консультация врача ФРМ",
    SpecialistRole.NEUROLOGIST: "Консультация невролога",
    SpecialistRole.PHYSICAL_THERAPIST: "Консультация специалиста по физической реабилитации",
    SpecialistRole.OCCUPATIONAL_THERAPIST: "Консультация специалиста по эргореабилитации",
    SpecialistRole.LOGOPEDIST: "Консультация медицинского логопеда",
    SpecialistRole.NEUROPSYCHOLOGIST: "Консультация медицинского психолога (нейропсихолога)",
    SpecialistRole.PATHOPSYCHOLOGIST: "Консультация медицинского психолога (патопсихолога)",
}

GENERIC_SPECIALTIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("психиатр",), "Консультация психиатра"),
    (("хирург",), "Консультация хирурга"),
    (("терапевт",), "Консультация терапевта"),
    (("эндокринолог",), "Консультация эндокринолога"),
    (("кардиолог",), "Консультация кардиолога"),
    (("уролог",), "Консультация уролога"),
    (("гастроэнтеролог",), "Консультация гастроэнтеролога"),
    (("рефлексотерапевт",), "Консультация рефлексотерапевта"),
    (("офтальмолог",), "Консультация офтальмолога"),
    (("оториноларинголог", "лор-врач", "лор врач"), "Консультация оториноларинголога"),
)

_CONSULTATION_HEADING_RE = re.compile(
    r"\b(?:первичн\w*|повторн\w*|заключительн\w*|итогов\w*)?\s*"
    r"(?:консультаци\w*|осмотр\w*|обследован\w*)\b",
    re.IGNORECASE,
)
_NAME_INITIALS_RE = re.compile(
    r"\b([А-ЯЁ][А-ЯЁа-яё-]{2,})\s+([А-ЯЁ])\.\s*([А-ЯЁ])\.",
)
_INITIALS_NAME_RE = re.compile(
    r"\b([А-ЯЁ])\.\s*([А-ЯЁ])\.\s*([А-ЯЁ][А-ЯЁа-яё-]{2,})\b",
)


def _leading_text(document: ParsedDocument) -> str:
    return clean_text(" ".join(document.paragraphs[:24]))[:5000]


def _consultation_intervention(
    document: ParsedDocument,
    classification: DocumentClassification,
) -> str | None:
    leading = _leading_text(document)
    if not _CONSULTATION_HEADING_RE.search(leading):
        return None
    if classification.role in ROLE_INTERVENTIONS:
        return ROLE_INTERVENTIONS[classification.role]
    low = leading.casefold().replace("ё", "е")
    for tokens, intervention in GENERIC_SPECIALTIES:
        if any(token.replace("ё", "е") in low for token in tokens):
            return intervention
    return None


def _performer(document: ParsedDocument) -> str:
    text = clean_text(document.text)
    matches: list[tuple[int, str]] = []
    for match in _NAME_INITIALS_RE.finditer(text):
        matches.append((match.start(), f"{match.group(1)} {match.group(2)}.{match.group(3)}."))
    for match in _INITIALS_NAME_RE.finditer(text):
        matches.append((match.start(), f"{match.group(3)} {match.group(1)}.{match.group(2)}."))
    return max(matches, default=(-1, ""), key=lambda item: item[0])[1]


def _treating_neurologist_performer(document: ParsedDocument) -> str:
    for line in document.text.splitlines():
        low = line.casefold()
        if "лечащ" not in low or "невролог" not in low:
            continue
        match = _NAME_INITIALS_RE.search(line)
        if match:
            return f"{match.group(1)} {match.group(2)}.{match.group(3)}."
        reverse = _INITIALS_NAME_RE.search(line)
        if reverse:
            return f"{reverse.group(3)} {reverse.group(1)}.{reverse.group(2)}."
    return ""


def _planned_dates(document: ParsedDocument) -> dict[str, date]:
    """Read only dates explicitly adjacent to a planned intervention.

    No admission-derived or workflow-derived date is invented here.  This is
    intentionally fail-closed because the source form is later signed.
    """

    lines = [clean_text(line) for line in document.text.splitlines() if clean_text(line)]
    result: dict[str, date] = {}
    intervention_tokens: list[tuple[str, tuple[str, ...]]] = [
        (ROLE_INTERVENTIONS[role], tokens)
        for role, tokens in (
            (SpecialistRole.FRM, ("фрм", "реабилитационной медицины")),
            (SpecialistRole.NEUROLOGIST, ("невролог",)),
            (SpecialistRole.PHYSICAL_THERAPIST, ("физическ", "лфк")),
            (SpecialistRole.OCCUPATIONAL_THERAPIST, ("эргореабил", "эрготерап")),
            (SpecialistRole.LOGOPEDIST, ("логопед",)),
            (SpecialistRole.NEUROPSYCHOLOGIST, ("нейропсих",)),
            (SpecialistRole.PATHOPSYCHOLOGIST, ("патопсих",)),
        )
    ]
    intervention_tokens.extend(
        (intervention, tokens) for tokens, intervention in GENERIC_SPECIALTIES
    )
    intervention_tokens.append(("Консилиум МДРК", ("консилиум мдрк", "консилиум мультидисциплинар")))

    for line in lines:
        low = line.casefold().replace("ё", "е")
        if not re.search(
            r"\b(?:назначен[аоы]?|запланирован[аоы]?|планов\w*|направлен[аоы]?\s+на)\b",
            low,
        ):
            continue
        parsed = parse_first_datetime(line)
        if parsed is None:
            continue
        for intervention, tokens in intervention_tokens:
            normalized_tokens = tuple(token.replace("ё", "е") for token in tokens)
            if any(token in low for token in normalized_tokens):
                result.setdefault(intervention, parsed.date())
    return result


def _is_primary_neurologist(classification: DocumentClassification) -> bool:
    return (
        classification.role is SpecialistRole.NEUROLOGIST
        and classification.document_type == "initial"
    )


def _existing_mdrk_rows(document: ParsedDocument) -> list[ReverseSheetRow]:
    rows: list[ReverseSheetRow] = []
    for table in document.tables:
        for source_row in table.rows:
            values = [clean_text(value) for value in source_row.as_list()]
            if not values or "консилиум" not in values[0].casefold() or "мдрк" not in values[0].casefold():
                continue
            parsed_appointment = parse_first_datetime(values[1]) if len(values) > 1 else None
            appointment = parsed_appointment.date() if parsed_appointment else None
            performed = None
            for index in (3, 4):
                if index < len(values) and (candidate := parse_first_datetime(values[index])) is not None:
                    performed = candidate
                    break
            performer = values[5] if len(values) > 5 else ""
            rows.append(
                ReverseSheetRow(
                    "Консилиум МДРК",
                    appointment,
                    performed,
                    performer,
                    document.source_path,
                )
            )
    return rows


def scan_reverse_sheet(
    folder: Path,
    *,
    normalizer: DocumentNormalizer | None = None,
) -> ReverseSheetDraft:
    folder = folder.resolve()
    draft = ReverseSheetDraft(folder=folder)
    source_files = discover_source_files(folder)
    owns_normalizer = normalizer is None
    normalizer = normalizer or DocumentNormalizer()
    parsed: list[tuple[ParsedDocument, DocumentClassification]] = []
    try:
        for source_path in source_files:
            try:
                normalized = normalizer.normalize(source_path)
                document = read_docx(normalized, source_path=source_path)
                parsed.append((document, classify_document(document)))
            except (ConversionError, OSError, ValueError, KeyError) as exc:
                draft.issues.append(
                    ReviewIssue(
                        "reverse_source_read_failed",
                        f"Не удалось прочитать {source_path.name}: {exc}",
                        ReviewSeverity.WARNING,
                        "sources",
                        source_path,
                    )
                )
    finally:
        if owns_normalizer:
            normalizer.close()

    primary_candidates = [item for item in parsed if _is_primary_neurologist(item[1])]
    existing_mdrk_rows = [
        row
        for document, classification in parsed
        if classification.document_type == "administrative"
        for row in _existing_mdrk_rows(document)
    ]
    existing_mdrk_rows.sort(
        key=lambda row: (row.performed_at is None, row.performed_at or datetime.max)
    )
    primary_candidates.sort(
        key=lambda item: (
            extract_clinical_datetime(item[0]) or datetime.max,
            str(item[0].source_path).casefold(),
        )
    )
    primary = primary_candidates[0][0] if primary_candidates else None
    planned: dict[str, date] = {}
    primary_performer = ""
    primary_clinical_date: date | None = None
    if primary is None:
        draft.issues.append(
            ReviewIssue(
                "reverse_primary_neurologist_missing",
                "Не найдена первичная консультация невролога: шапка оставлена пустой.",
                ReviewSeverity.BLOCKING,
                "header",
            )
        )
    else:
        draft.header_source = primary.source_path
        draft.identity = extract_patient_identity(primary)
        draft.admission_datetime = extract_admission_datetime(primary)
        planned = _planned_dates(primary)
        primary_performer = _treating_neurologist_performer(primary)
        primary_datetime = extract_clinical_datetime(primary)
        primary_clinical_date = primary_datetime.date() if primary_datetime is not None else None

    rows: list[ReverseSheetRow] = []
    mdrk_index = 0
    for document, classification in parsed:
        if classification.is_mdrk:
            performed_at = extract_mdrk_document_datetime(document)
            intervention = "Консилиум МДРК"
            existing = (
                existing_mdrk_rows[min(mdrk_index, len(existing_mdrk_rows) - 1)]
                if existing_mdrk_rows
                else None
            )
            mdrk_index += 1
            if performed_at is None and existing is not None:
                performed_at = existing.performed_at
                draft.issues.append(
                    ReviewIssue(
                        "reverse_mdrk_date_carried_from_existing_sheet",
                        "Даты консилиума МДРК перенесены из существующего оборотного листа; проверьте их.",
                        ReviewSeverity.INFO,
                        "rows",
                        existing.source,
                    )
                )
            if performed_at is None:
                draft.issues.append(
                    ReviewIssue(
                        "reverse_mdrk_date_missing",
                        f"В {document.source_path.name} не найдена дата консилиума МДРК.",
                        ReviewSeverity.WARNING,
                        "rows",
                        document.source_path,
                    )
                )
            rows.append(
                ReverseSheetRow(
                    intervention,
                    performed_at.date()
                    if performed_at is not None
                    else (
                        existing.appointment_date
                        if existing is not None
                        else planned.get(intervention) or planned.get("Консилиум МДРК")
                    ),
                    performed_at,
                    primary_performer or (existing.performer if existing is not None else ""),
                    document.source_path,
                )
            )
            continue
        # Neurologist documents supply the episode header and clinical MDRK
        # content, but are never separate interventions on the reverse sheet.
        if classification.role is SpecialistRole.NEUROLOGIST:
            continue
        if classification.document_type in {
            "administrative",
            "assignment_sheet",
            "other_consilium",
            "unknown",
        }:
            continue
        intervention = _consultation_intervention(document, classification)
        if intervention is None:
            continue
        performed_at = extract_clinical_datetime(document)
        is_repeat = classification.document_type in {"follow_up", "final"}
        if classification.document_type == "initial":
            appointment_date = primary_clinical_date or planned.get(intervention)
        elif is_repeat and performed_at is not None:
            appointment_date = performed_at.date()
        else:
            appointment_date = planned.get(intervention)
        rows.append(
            ReverseSheetRow(
                intervention,
                appointment_date,
                performed_at,
                _performer(document),
                document.source_path,
            )
        )

    deduplicated: dict[tuple[str, datetime | None, str], ReverseSheetRow] = {}
    for row in rows:
        key = (row.intervention.casefold(), row.performed_at, row.performer.casefold())
        deduplicated.setdefault(key, row)
    draft.rows = sorted(
        deduplicated.values(),
        key=lambda row: (
            row.performed_at is None,
            row.performed_at or datetime.max,
            row.intervention.casefold(),
            str(row.source or "").casefold(),
        ),
    )
    for row in draft.rows:
        if row.performed_at is None:
            draft.issues.append(
                ReviewIssue(
                    "reverse_execution_date_missing",
                    f"Не найдена дата исполнения: {row.intervention}.",
                    ReviewSeverity.WARNING,
                    "rows",
                    row.source,
                )
            )
    return draft
