from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class EpisodeCompatibility(Enum):
    VERIFIED = "verified"
    INSUFFICIENT = "insufficient"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class EpisodeMatch:
    compatibility: EpisodeCompatibility
    medical_record_number: bool | None
    full_name: bool | None
    admission: bool | None
    discharge: bool | None
    episode_root: bool | None
    conflicts: tuple[str, ...] = ()

    def confirms_episode(self, *, dated_point: bool = False) -> bool:
        return self.medical_record_number is True or (
            self.full_name is True
            and (
                self.admission is True
                or self.discharge is True
                or dated_point
            )
        )

    def confirms_source_projection(self, *, dated_point: bool) -> bool:
        return self.confirms_episode(dated_point=dated_point) or (
            self.episode_root is True and dated_point
        )


@dataclass(frozen=True, slots=True)
class DischargeEpisodeKey:
    normalized_full_name: str = ""
    medical_record_number: str = ""
    admission_at: datetime | None = None
    discharge_at: datetime | None = None
    episode_root: Path | None = None

    def match(self, other: "DischargeEpisodeKey") -> EpisodeMatch:
        medical_record_match = _optional_match(
            self.medical_record_number,
            other.medical_record_number,
        )
        full_name_match = _optional_match(
            self.normalized_full_name,
            other.normalized_full_name,
        )
        admission_match = _optional_date_match(
            self.admission_at,
            other.admission_at,
        )
        discharge_match = _optional_date_match(
            self.discharge_at,
            other.discharge_at,
        )
        episode_root_match = _optional_match(
            self.episode_root,
            other.episode_root,
        )
        conflicts = tuple(
            field_name
            for field_name, matches in (
                ("medical_record_number", medical_record_match),
                ("full_name", full_name_match),
                ("admission_at", admission_match),
                ("discharge_at", discharge_match),
            )
            if matches is False
        )
        if (
            episode_root_match is False
            and medical_record_match is not True
            and full_name_match is not True
        ):
            conflicts += ("episode_root",)
        if conflicts:
            compatibility = EpisodeCompatibility.CONFLICT
        elif medical_record_match is True or full_name_match is True:
            compatibility = EpisodeCompatibility.VERIFIED
        else:
            compatibility = EpisodeCompatibility.INSUFFICIENT
        return EpisodeMatch(
            compatibility=compatibility,
            medical_record_number=medical_record_match,
            full_name=full_name_match,
            admission=admission_match,
            discharge=discharge_match,
            episode_root=episode_root_match,
            conflicts=conflicts,
        )

    def merged_with(self, other: "DischargeEpisodeKey") -> "DischargeEpisodeKey":
        match = self.match(other)
        if match.compatibility is not EpisodeCompatibility.VERIFIED:
            raise ValueError("episode keys are not positively compatible")
        return DischargeEpisodeKey(
            normalized_full_name=(
                self.normalized_full_name or other.normalized_full_name
            ),
            medical_record_number=(
                self.medical_record_number or other.medical_record_number
            ),
            admission_at=self.admission_at or other.admission_at,
            discharge_at=self.discharge_at or other.discharge_at,
            episode_root=self.episode_root or other.episode_root,
        )

    def contains(self, occurred_at: datetime) -> bool:
        occurred_on = occurred_at.date()
        if self.admission_at is not None and occurred_on < self.admission_at.date():
            return False
        return (
            self.discharge_at is None
            or occurred_on <= self.discharge_at.date()
        )


def resolve_episode_root(
    scan_root: Path | None,
    source_files: tuple[Path, ...],
    source_path: Path,
) -> Path:
    if scan_root is None:
        return source_path.parent
    scan_root = scan_root.resolve()
    if any(path.resolve().parent == scan_root for path in source_files):
        return scan_root
    try:
        relative = source_path.resolve().relative_to(scan_root)
    except ValueError:
        return source_path.parent
    return (
        scan_root / relative.parts[0]
        if len(relative.parts) > 1
        else scan_root
    )


def _optional_match(left: object, right: object) -> bool | None:
    if not left or not right:
        return None
    return left == right


def _optional_date_match(
    left: datetime | None,
    right: datetime | None,
) -> bool | None:
    if left is None or right is None:
        return None
    return left.date() == right.date()
