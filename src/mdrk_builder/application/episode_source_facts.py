from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mdrk_builder.application.discharge_extractors import (
    extract_summary_discharge_datetime,
)
from mdrk_builder.application.episode_identity import (
    DischargeEpisodeKey,
    resolve_episode_root,
)
from mdrk_builder.application.extractors import (
    extract_admission_datetime,
    extract_discharge_datetime,
    extract_patient_identity,
)
from mdrk_builder.application.identifiers import (
    normalize_medical_record_number,
    normalize_patient_full_name,
)
from mdrk_builder.application.source_scan import SourceScanResult
from mdrk_builder.domain import PatientIdentity
from mdrk_builder.infrastructure.ooxml_reader import ParsedDocument


@dataclass(frozen=True, slots=True)
class EpisodeDocumentFacts:
    identity: PatientIdentity
    admission_at: datetime | None
    discharge_at: datetime | None
    episode_key: DischargeEpisodeKey


def episode_facts_from_document(
    source_scan: SourceScanResult,
    document: ParsedDocument,
) -> EpisodeDocumentFacts:
    identity = extract_patient_identity(document)
    admission_at = extract_admission_datetime(document)
    discharge_at = (
        extract_discharge_datetime(document)
        or extract_summary_discharge_datetime(document)
    )
    episode_key = DischargeEpisodeKey(
        normalized_full_name=normalize_patient_full_name(identity.full_name),
        medical_record_number=normalize_medical_record_number(
            identity.medical_record_number
        ),
        admission_at=admission_at,
        discharge_at=discharge_at,
        episode_root=resolve_episode_root(
            source_scan.root,
            source_scan.source_files,
            document.source_path,
        ),
    )
    return EpisodeDocumentFacts(
        identity=identity,
        admission_at=admission_at,
        discharge_at=discharge_at,
        episode_key=episode_key,
    )
