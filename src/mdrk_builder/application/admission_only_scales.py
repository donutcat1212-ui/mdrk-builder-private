"""The department records eGFR only in MDRK-1, including copied prose rows."""
from dataclasses import fields, replace
import re

from mdrk_builder.application.scale_registry import canonical_scale_key
from mdrk_builder.domain import DischargeSummaryDraft

_EGFR_ITEM = re.compile(
    r"(?im)(?:^|(?<=;))[ \t]*(?:(?:\d+[.)]|[-•–])\s*)?"
    r"(?:СКФ|eGFR|скорость\s+клубочковой\s+фильтрации)\b[^;\n]*(?:;|\n|$)"
)


def is_admission_only_scale(name: str) -> bool:
    return canonical_scale_key(name) == "egfr"


def without_admission_scale_items(text: str) -> str:
    """Remove a labelled measurement item, retaining neighbouring clinical facts."""
    return _EGFR_ITEM.sub("", text)


def omit_admission_scales_from_discharge(draft: DischargeSummaryDraft) -> None:
    for field in fields(draft):
        value = getattr(draft, field.name)
        if isinstance(value, str):
            setattr(draft, field.name, without_admission_scale_items(value))
    for name in ("admission_scale_rows", "discharge_scale_rows"):
        setattr(draft, name, tuple(row for row in getattr(draft, name)
                                  if not is_admission_only_scale(row.name)))
    draft.team_findings = tuple(replace(
        finding, conclusion=without_admission_scale_items(finding.conclusion),
        scales=tuple(row for row in finding.scales if not is_admission_only_scale(row.name)),
    ) for finding in draft.team_findings)
