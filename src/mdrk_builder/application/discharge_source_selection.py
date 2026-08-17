from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

from mdrk_builder.application.episode_identity import (
    DischargeEpisodeKey,
    EpisodeCompatibility,
    EpisodeMatch,
)
from mdrk_builder.application.episode_source_facts import (
    episode_facts_from_document,
)
from mdrk_builder.application.extractors import (
    extract_clinical_datetime,
    extract_clinical_sections,
    extract_mdrk_document_datetime,
    extract_procedures,
)
from mdrk_builder.application.source_scan import (
    ScannedDocument,
    SourceScanResult,
)
from mdrk_builder.domain import (
    MdrkKind,
    PatientIdentity,
    ReviewIssue,
    ReviewSeverity,
    SpecialistRole,
)


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    scanned: ScannedDocument
    identity: PatientIdentity
    admission_at: datetime | None
    discharge_at: datetime | None
    sections: dict[str, str]
    episode_key: DischargeEpisodeKey

    @property
    def path(self) -> Path:
        return self.scanned.document.source_path


@dataclass(frozen=True, slots=True)
class SourceSelection:
    discharge: SourceCandidate | None
    primary: SourceCandidate | None
    episode_key: DischargeEpisodeKey | None
    issues: tuple[ReviewIssue, ...] = ()

    @property
    def medical_record_number(self) -> str | None:
        for candidate in (self.discharge, self.primary):
            if candidate and candidate.identity.medical_record_number.strip():
                return candidate.identity.medical_record_number.strip()
        return None


@dataclass(frozen=True, slots=True)
class _SourcePair:
    discharge: SourceCandidate
    primary: SourceCandidate
    match: EpisodeMatch


def select_discharge_sources(source_scan: SourceScanResult) -> SourceSelection:
    primary_roles = {SpecialistRole.NEUROLOGIST, SpecialistRole.FRM}
    discharges = [
        _candidate(source_scan, item)
        for item in source_scan.documents
        if item.classification.is_discharge_summary
    ]
    primaries = [
        _candidate(source_scan, item)
        for item in source_scan.documents
        if item.classification.role in primary_roles
        and item.classification.document_type == "initial"
        and not item.classification.is_discharge_summary
        and not item.classification.is_generated_output
        and not _is_admission_department_source(item)
    ]
    pairs = [
        _SourcePair(discharge, primary, match)
        for discharge in discharges
        for primary in primaries
        if (
            match := discharge.episode_key.match(primary.episode_key)
        ).compatibility
        is EpisodeCompatibility.VERIFIED
        and match.confirms_episode()
    ]
    narrowed_pairs = _narrow_pairs(pairs)
    if len(narrowed_pairs) == 1:
        pair = narrowed_pairs[0]
        return SourceSelection(
            discharge=pair.discharge,
            primary=pair.primary,
            episode_key=pair.discharge.episode_key.merged_with(
                pair.primary.episode_key
            ),
        )
    if narrowed_pairs:
        discharge_paths = {pair.discharge.path for pair in narrowed_pairs}
        primary_paths = {pair.primary.path for pair in narrowed_pairs}
        discharge = (
            narrowed_pairs[0].discharge if len(discharge_paths) == 1 else None
        )
        primary = narrowed_pairs[0].primary if len(primary_paths) == 1 else None
        episode_key = (
            discharge.episode_key
            if discharge is not None
            else primary.episode_key if primary is not None else None
        )
        return SourceSelection(
            discharge=discharge,
            primary=primary,
            episode_key=episode_key,
            issues=(
                _selection_issue(
                    "episode_source_selection_ambiguous",
                    (
                        "Найдено несколько источников одного эпизода, но нельзя "
                        "однозначно выбрать выписной эпикриз и первичный осмотр."
                    ),
                    [*discharges, *primaries],
                ),
            ),
        )

    if not discharges and not primaries:
        return SourceSelection(None, None, None)
    if not discharges:
        primary = primaries[0] if len(primaries) == 1 else None
        issues = () if primary is not None else (
            _selection_issue(
                "episode_source_selection_ambiguous",
                "Найдено несколько первичных осмотров разных или неясных эпизодов.",
                primaries,
            ),
        )
        return SourceSelection(
            None,
            primary,
            primary.episode_key if primary is not None else None,
            issues,
        )
    if not primaries:
        discharge = discharges[0] if len(discharges) == 1 else None
        issues = () if discharge is not None else (
            _selection_issue(
                "episode_source_selection_ambiguous",
                "Найдено несколько выписных эпикризов разных или неясных эпизодов.",
                discharges,
            ),
        )
        return SourceSelection(
            discharge,
            None,
            discharge.episode_key if discharge is not None else None,
            issues,
        )

    comparisons = [
        discharge.episode_key.match(primary.episode_key)
        for discharge in discharges
        for primary in primaries
    ]
    conflict = any(
        match.compatibility is EpisodeCompatibility.CONFLICT
        for match in comparisons
    )
    discharge = discharges[0] if len(discharges) == 1 else None
    primary = (
        primaries[0]
        if discharge is None and len(primaries) == 1
        else None
    )
    issue = _selection_issue(
        (
            "episode_source_identity_conflict"
            if conflict
            else "episode_source_identity_insufficient"
        ),
        (
            "Выписной эпикриз и первичный осмотр относятся к разным эпизодам."
            if conflict
            else (
                "Недостаточно ФИО или номера медицинской карты, чтобы безопасно "
                "связать выписной эпикриз с первичным осмотром."
            )
        ),
        [*discharges, *primaries],
    )
    selected_candidate = discharge or primary
    return SourceSelection(
        discharge=discharge,
        primary=primary,
        episode_key=(
            selected_candidate.episode_key
            if selected_candidate is not None
            else None
        ),
        issues=(issue,),
    )


def source_scan_for_episode(
    source_scan: SourceScanResult,
    episode_key: DischargeEpisodeKey | None,
    *,
    issues: list[ReviewIssue] | None = None,
) -> SourceScanResult:
    if episode_key is None:
        documents: tuple[ScannedDocument, ...] = ()
    else:
        selected: list[ScannedDocument] = []
        for scanned in source_scan.documents:
            candidate = _candidate(source_scan, scanned)
            candidate_key = candidate.episode_key
            match = episode_key.match(candidate_key)
            if match.compatibility is EpisodeCompatibility.CONFLICT:
                continue
            if (
                match.compatibility is EpisodeCompatibility.INSUFFICIENT
                and match.episode_root is not True
            ):
                continue
            if not _contributes_to_episode_scan(scanned):
                selected.append(scanned)
                continue
            occurred_ats = _source_occurrence_datetimes(scanned, episode_key)
            if (
                scanned.classification.document_type == "assignment_sheet"
                and not occurred_ats
            ):
                _record_projection_issue(
                    issues,
                    "episode_assignment_sheet_date_missing",
                    (
                        "Лист назначений без извлекаемой даты выполнения исключён "
                        "из выписного эпизода."
                    ),
                    candidate.path,
                )
                continue
            if episode_key.admission_at is not None and any(
                occurred_at.date() < episode_key.admission_at.date()
                for occurred_at in occurred_ats
            ):
                _record_projection_issue(
                    issues,
                    "episode_source_before_admission_excluded",
                    (
                        "Источник датирован раньше поступления и исключён "
                        "из текущей госпитализации."
                    ),
                    candidate.path,
                )
                continue
            if episode_key.discharge_at is not None and any(
                occurred_at.date() > episode_key.discharge_at.date()
                for occurred_at in occurred_ats
            ):
                _record_projection_issue(
                    issues,
                    "episode_source_after_discharge_excluded",
                    (
                        "Источник датирован позже выписки и исключён "
                        "из текущей госпитализации."
                    ),
                    candidate.path,
                )
                continue
            dated_point = bool(
                occurred_ats
                or candidate.admission_at is not None
                or candidate.discharge_at is not None
            )
            if not match.confirms_source_projection(dated_point=dated_point):
                _record_projection_issue(
                    issues,
                    "episode_source_identity_and_date_missing",
                    (
                        "Источник без номера медицинской карты и датированной "
                        "привязки исключён из текущей госпитализации."
                    ),
                    candidate.path,
                )
                continue
            selected.append(scanned)
        documents = tuple(selected)
    return SourceScanResult(
        source_files=source_scan.source_files,
        documents=documents,
        failures=source_scan.failures,
        root=source_scan.root,
    )


def _contributes_to_episode_scan(scanned: ScannedDocument) -> bool:
    classification = scanned.classification
    return not (
        classification.is_generated_output
        or classification.is_discharge_summary
        or (
            classification.is_mdrk
            and classification.mdrk_kind is not MdrkKind.INITIAL
        )
    )


def _source_occurrence_datetimes(
    scanned: ScannedDocument,
    episode_key: DischargeEpisodeKey,
) -> tuple[datetime, ...]:
    if scanned.classification.is_mdrk:
        occurred_at = extract_mdrk_document_datetime(scanned.document)
        return (occurred_at,) if occurred_at is not None else ()

    occurred_ats = []
    if occurred_at := extract_clinical_datetime(scanned.document):
        occurred_ats.append(occurred_at)
    if scanned.classification.document_type == "assignment_sheet":
        reference_date = (
            episode_key.admission_at.date()
            if episode_key.admission_at is not None
            else None
        )
        occurred_ats.extend(
            datetime.combine(performed_date, time.min)
            for procedure in extract_procedures(
                scanned.document,
                reference_date=reference_date,
            )
            for performed_date in procedure.performed_dates
        )
    return tuple(sorted(set(occurred_ats)))


def _record_projection_issue(
    issues: list[ReviewIssue] | None,
    code: str,
    message: str,
    source: Path,
) -> None:
    if issues is None:
        return
    issues.append(
        ReviewIssue(
            code,
            message,
            ReviewSeverity.WARNING,
            "episode_sources",
            source,
        )
    )


def _candidate(
    source_scan: SourceScanResult,
    scanned: ScannedDocument,
) -> SourceCandidate:
    document = scanned.document
    facts = episode_facts_from_document(source_scan, document)
    return SourceCandidate(
        scanned=scanned,
        identity=facts.identity,
        admission_at=facts.admission_at,
        discharge_at=facts.discharge_at,
        sections=extract_clinical_sections(document),
        episode_key=facts.episode_key,
    )


def _selection_issue(
    code: str,
    message: str,
    candidates: list[SourceCandidate],
) -> ReviewIssue:
    source = candidates[0].path if candidates else None
    return ReviewIssue(
        code,
        message,
        ReviewSeverity.BLOCKING,
        "episode_sources",
        source,
    )


def _narrow_pairs(pairs: list[_SourcePair]) -> list[_SourcePair]:
    medical_record_pairs = [
        pair for pair in pairs if pair.match.medical_record_number is True
    ]
    if medical_record_pairs:
        pairs = medical_record_pairs
    admission_pairs = [pair for pair in pairs if pair.match.admission is True]
    return admission_pairs or pairs


def _is_admission_department_source(scanned: ScannedDocument) -> bool:
    document = scanned.document
    source_name = document.source_path.name.casefold().replace("ё", "е")
    if "приемн" in source_name:
        return True
    for line in document.text.splitlines()[:12]:
        heading = line.casefold().replace("ё", "е")
        if (
            ":" not in heading
            and "приемн" in heading
            and ("первичн" in heading or "осмотр" in heading)
        ):
            return True
    return False
