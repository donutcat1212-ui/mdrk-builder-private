from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from mdrk_builder.application.episode_identity import (
    DischargeEpisodeKey,
    EpisodeCompatibility,
)
from mdrk_builder.application.episode_source_facts import (
    episode_facts_from_document,
)
from mdrk_builder.application.extractors import (
    extract_clinical_sections,
    extract_icf_observations,
    extract_mdrk_document_datetime,
    extract_mdrk_scale_measurements,
)
from mdrk_builder.application.scale_registry import (
    canonical_scale_key,
    numeric_scale_value,
    scale_bounds,
)
from mdrk_builder.application.source_scan import ScannedDocument, SourceScanResult
from mdrk_builder.domain import (
    Episode,
    IcfDomain,
    MdrkKind,
    ReviewIssue,
    ReviewSeverity,
    ScaleMeasurement,
    SourceDocument,
    SpecialistFinding,
    SpecialistRole,
)
from mdrk_builder.infrastructure.ooxml_reader import clean_text


_COMPLETED_PROGRAM_RE = re.compile(
    r"\b(?:выполненн|проведенн)\w*\s+программ\w*\s+медицинск\w*\s+"
    r"реабилитац\w*",
    re.IGNORECASE,
)


def is_structurally_final_mdrk(scanned: ScannedDocument) -> bool:
    return bool(_COMPLETED_PROGRAM_RE.search(clean_text(scanned.document.text)))


@dataclass(frozen=True, slots=True)
class _FinalMdrkCandidate:
    scanned: ScannedDocument
    measured_at: datetime | None


def select_final_mdrk_document(
    source_scan: SourceScanResult,
    *,
    episode_key: DischargeEpisodeKey,
    issues: list[ReviewIssue] | None = None,
) -> ScannedDocument | None:
    dated: list[_FinalMdrkCandidate] = []
    undated: list[_FinalMdrkCandidate] = []
    for scanned in source_scan.documents:
        classification = scanned.classification
        if (
            not classification.is_mdrk
            or classification.mdrk_kind is not MdrkKind.FINAL
            or classification.is_discharge_summary
            or not is_structurally_final_mdrk(scanned)
        ):
            continue
        candidate_key = episode_facts_from_document(
            source_scan,
            scanned.document,
        ).episode_key
        match = episode_key.match(candidate_key)
        if match.compatibility is not EpisodeCompatibility.VERIFIED:
            continue
        measured_at = extract_mdrk_document_datetime(scanned.document)
        if measured_at is not None and not episode_key.contains(measured_at):
            continue
        if not match.confirms_episode(dated_point=measured_at is not None):
            continue
        candidate = _FinalMdrkCandidate(scanned, measured_at)
        (dated if measured_at is not None else undated).append(candidate)
    if dated:
        latest_at = max(candidate.measured_at for candidate in dated)
        latest = [
            candidate for candidate in dated if candidate.measured_at == latest_at
        ]
        if len(latest) == 1:
            return latest[0].scanned
        _report_ambiguous_final_mdrk(latest, issues)
        return None
    if len(undated) == 1:
        return undated[0].scanned
    if undated:
        _report_ambiguous_final_mdrk(undated, issues)
    return None


def _report_ambiguous_final_mdrk(
    candidates: list[_FinalMdrkCandidate],
    issues: list[ReviewIssue] | None,
) -> None:
    if issues is None:
        return
    issues.append(
        ReviewIssue(
            "final_mdrk_source_ambiguous",
            (
                "Найдено несколько равнозначных итоговых МДРК-2; "
                "автоматический выбор заблокирован."
            ),
            ReviewSeverity.BLOCKING,
            "final_mdrk_source",
            candidates[0].scanned.document.source_path,
        )
    )


def apply_final_mdrk_document(
    episode: Episode,
    scanned: ScannedDocument,
    *,
    discharge_scale_values: dict[str, str],
    issues: list[ReviewIssue],
) -> None:
    document = scanned.document
    measured_at = extract_mdrk_document_datetime(document) or episode.final_meeting_at
    measurements = validate_final_scale_measurements(
        extract_mdrk_scale_measurements(document, measured_at),
        discharge_scale_values,
        source=document.source_path,
        issues=issues,
    )
    previous_scales: dict[
        tuple[str, str],
        list[tuple[SpecialistFinding, ScaleMeasurement]],
    ] = defaultdict(list)
    for finding in episode.findings:
        for measurement in finding.scales:
            previous_scales[
                (_role_group(measurement.specialist), canonical_scale_key(measurement.name))
            ].append((finding, measurement))

    for measurement in measurements:
        key = (
            _role_group(measurement.specialist),
            canonical_scale_key(measurement.name),
        )
        if candidates := previous_scales.get(key):
            _finding, baseline = min(
                candidates,
                key=lambda item: (
                    item[1].measured_at
                    or item[0].source_datetime
                    or datetime.max,
                    str(item[1].source or item[0].source or "").casefold(),
                ),
            )
            measurement.specialist = baseline.specialist

    authoritative_keys = {
        (
            _role_group(measurement.specialist),
            canonical_scale_key(measurement.name),
        )
        for measurement in measurements
    }
    final_starts: dict[tuple[str, str], datetime] = {}
    for measurement in measurements:
        key = (
            _role_group(measurement.specialist),
            canonical_scale_key(measurement.name),
        )
        point = measurement.measured_at or measured_at
        if point is not None and (key not in final_starts or point < final_starts[key]):
            final_starts[key] = point

    def precedes_final(
        finding: SpecialistFinding,
        measurement: ScaleMeasurement,
    ) -> bool:
        key = (
            _role_group(measurement.specialist),
            canonical_scale_key(measurement.name),
        )
        point = measurement.measured_at or finding.source_datetime
        return (
            key not in authoritative_keys
            or key not in final_starts
            or point is None
            or point < final_starts[key]
        )

    for finding in episode.findings:
        finding.scales = [
            measurement
            for measurement in finding.scales
            if precedes_final(finding, measurement)
        ]
    by_role: dict[SpecialistRole, list[ScaleMeasurement]] = defaultdict(list)
    for measurement in measurements:
        by_role[measurement.specialist].append(measurement)
    for role, role_measurements in by_role.items():
        episode.findings.append(
            SpecialistFinding(
                role=role,
                source_datetime=measured_at,
                source=document.source_path,
                scales=role_measurements,
            )
        )

    missing_scale_keys = set(previous_scales) - authoritative_keys
    if missing_scale_keys or not measurements:
        labels = sorted(
            {
                item.name
                for key in missing_scale_keys
                for _finding, item in previous_scales[key]
            },
            key=str.casefold,
        )
        detail = f" Отсутствуют: {', '.join(labels)}." if labels else ""
        issues.append(
            ReviewIssue(
                "final_mdrk_scale_rows_missing",
                (
                    "В итоговом МДРК-2 отсутствуют отдельные шкалы или раздел "
                    f"шкал; их значения при выписке оставлены пустыми.{detail}"
                ),
                ReviewSeverity.WARNING,
                "discharge_scales",
                document.source_path,
            )
        )

    observations = extract_icf_observations(document)
    final_domains = [
        IcfDomain(
            code=observation.code,
            description=observation.description,
            specialist=observation.specialist or SpecialistRole.OTHER,
            initial=observation.ratings[0] if observation.ratings else None,
            final=(
                observation.ratings[-1]
                if len(observation.ratings) >= 2
                else None
            ),
            note=observation.note,
            initial_source=document.source_path,
            final_source=(
                document.source_path if len(observation.ratings) >= 2 else None
            ),
            initial_measured_at=episode.initial_meeting_at,
            final_measured_at=measured_at,
        )
        for observation in observations
    ]
    final_icf_keys = {_icf_key(domain.code, domain.description) for domain in final_domains}
    missing_icf_domains = [
        domain
        for domain in episode.icf_domains
        if _icf_key(domain.code, domain.description) not in final_icf_keys
    ]
    episode.icf_domains = [
        *final_domains,
        *(
            replace(
                domain,
                final=None,
                final_source=None,
                final_measured_at=None,
            )
            for domain in missing_icf_domains
        ),
    ]
    if missing_icf_domains or not observations:
        codes = sorted(
            {domain.code for domain in missing_icf_domains},
            key=str.casefold,
        )
        detail = f" Отсутствуют: {', '.join(codes)}." if codes else ""
        issues.append(
            ReviewIssue(
                "final_mdrk_icf_rows_missing",
                (
                    "В итоговом МДРК-2 отсутствуют отдельные домены или профиль "
                    f"МКФ; их итоговые оценки оставлены пустыми.{detail}"
                ),
                ReviewSeverity.WARNING,
                "rehabilitation_diagnosis",
                document.source_path,
            )
        )

    final_sections = extract_clinical_sections(document)
    for field_name in ("rehabilitation_potential", "goal"):
        value = final_sections[field_name]
        setattr(episode.sections, field_name, value)
        source_key = f"sections.{field_name}"
        if value:
            episode.field_sources[source_key] = document.source_path
        else:
            episode.field_sources.pop(source_key, None)

    if not any(source.path == document.source_path for source in episode.sources):
        episode.sources.append(
            SourceDocument(
                path=document.source_path,
                role=SpecialistRole.OTHER,
                clinical_datetime=measured_at,
                document_type="mdrk_final",
                extraction_method=(
                    "docx"
                    if document.source_path.suffix.casefold() == ".docx"
                    else "converted"
                ),
                sha256=document.sha256,
            )
        )


def validate_final_scale_measurements(
    measurements: list[ScaleMeasurement],
    discharge_values: dict[str, str],
    *,
    source: Path,
    issues: list[ReviewIssue],
) -> list[ScaleMeasurement]:
    corroborating: dict[str, dict[str, str]] = defaultdict(dict)
    for name, value in discharge_values.items():
        if not value.strip():
            continue
        numeric = numeric_scale_value(value)
        unique_value = (
            f"numeric:{numeric:g}"
            if numeric is not None
            else " ".join(value.casefold().replace("ё", "е").split())
        )
        corroborating[canonical_scale_key(name)].setdefault(unique_value, value)
    result: list[ScaleMeasurement] = []
    for measurement in measurements:
        bounds = scale_bounds(measurement.name)
        numeric = numeric_scale_value(measurement.value)
        if bounds is None or numeric is None or bounds[0] <= numeric <= bounds[1]:
            result.append(measurement)
            continue

        confirmations = corroborating.get(canonical_scale_key(measurement.name), {})
        replacement = next(iter(confirmations.values()), "") if len(confirmations) == 1 else ""
        replacement_numeric = numeric_scale_value(replacement)
        replacement_valid = (
            replacement_numeric is not None
            and bounds[0] <= replacement_numeric <= bounds[1]
        )
        if replacement_valid:
            result.append(replace(measurement, value=replacement))
            message = (
                f"В МДРК-2 значение «{measurement.name}» ({measurement.value}) вне "
                f"диапазона {bounds[0]}–{bounds[1]}; использовано подтверждающее "
                f"значение из текущего выписного эпикриза ({replacement})."
            )
        elif len(confirmations) > 1:
            result.append(measurement)
            message = (
                f"В МДРК-2 значение «{measurement.name}» ({measurement.value}) вне "
                f"диапазона {bounds[0]}–{bounds[1]}; в выписном эпикризе найдены "
                "конфликтующие подтверждения. Исходное значение сохранено."
            )
        else:
            result.append(measurement)
            message = (
                f"В МДРК-2 значение «{measurement.name}» ({measurement.value}) вне "
                f"диапазона {bounds[0]}–{bounds[1]}. Проверьте его вручную."
            )
        issues.append(
            ReviewIssue(
                "scale_value_out_of_range",
                message,
                ReviewSeverity.WARNING,
                "discharge_scales",
                source,
            )
        )
    return result


def _role_group(role: SpecialistRole) -> str:
    if role in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}:
        return "physician"
    return role.value


def _icf_key(code: str, description: str) -> tuple[str, str]:
    return (
        code.casefold().replace(" ", ""),
        " ".join(
            "".join(
                character if character.isalnum() else " "
                for character in description.casefold().replace("ё", "е")
            ).split()
        ),
    )
