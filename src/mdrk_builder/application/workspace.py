"""Serializable state of one open episode, including unfinished form input."""
from dataclasses import dataclass, field
from pathlib import Path

from mdrk_builder.domain import DischargeSummaryDraft, Episode, MdrkKind, ReverseSheetDraft


@dataclass
class MdrkWorkspaceState:
    episode: Episode
    baseline: Episode | None = None
    entry_fields: set[str] = field(default_factory=set)
    section_fields: dict[MdrkKind, set[str]] = field(default_factory=dict)
    collections: set[str] = field(default_factory=set)
    entries: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)


@dataclass
class ReverseWorkspaceState:
    draft: ReverseSheetDraft
    baseline: ReverseSheetDraft | None = None
    rows_dirty: bool = False
    header_dirty: set[str] = field(default_factory=set)
    entries: dict[str, str] = field(default_factory=dict)


@dataclass
class DischargeWorkspaceState:
    draft: DischargeSummaryDraft
    baseline: DischargeSummaryDraft | None = None
    dirty_fields: set[str] = field(default_factory=set)
    dirty_identity: set[str] = field(default_factory=set)
    entries: dict[str, str] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkspaceDraft:
    kind: MdrkKind
    document: str
    mdrk: MdrkWorkspaceState | None = None
    reverse: ReverseWorkspaceState | None = None
    discharge: DischargeWorkspaceState | None = None

    @property
    def folder(self) -> Path | None:
        if self.mdrk is not None:
            return self.mdrk.episode.folder
        if self.discharge is not None:
            return self.discharge.draft.folder
        if self.reverse is not None:
            return self.reverse.draft.folder
        return None

    def validate(self, folder: Path) -> None:
        if not isinstance(self.kind, MdrkKind) or self.document not in {'mdrk1', 'mdrk2', 'reverse', 'discharge'}:
            raise ValueError('Неизвестный вид документа в черновике')
        for state, state_type, model_type in (
            (self.mdrk, MdrkWorkspaceState, Episode),
            (self.reverse, ReverseWorkspaceState, ReverseSheetDraft),
            (self.discharge, DischargeWorkspaceState, DischargeSummaryDraft),
        ):
            if state is None:
                continue
            if not isinstance(state, state_type):
                raise ValueError('Неверный тип состояния документа')
            model = state.episode if isinstance(state, MdrkWorkspaceState) else state.draft
            for value in (model, state.baseline):
                if value is not None and (not isinstance(value, model_type) or value.folder.resolve() != folder.resolve()):
                    raise ValueError('Черновик или его исходные данные относятся к другой папке эпизода')
            mappings = [state.entries]
            if isinstance(state, MdrkWorkspaceState):
                mappings.append(state.sections)
            elif isinstance(state, DischargeWorkspaceState):
                mappings.append(state.text)
            if any(not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()) for mapping in mappings):
                raise ValueError('Неверный формат полей черновика')


def restore_workspace_state(value, folder: Path) -> WorkspaceDraft:
    """Read the original v1 dictionary once; unloaded panels contribute no state."""
    if isinstance(value, dict):
        manual = value.get('manual') or {}
        mdrk = None
        if value.get('episode') is not None:
            mdrk = MdrkWorkspaceState(
                value['episode'], value.get('baseline'), manual.get('entry_fields', set()),
                manual.get('section_fields', {}), manual.get('collections', set()),
                value.get('entries', {}), value.get('sections', {}),
            )
        reverse = None
        if value.get('reverse') is not None:
            reverse = ReverseWorkspaceState(
                value['reverse'], value.get('reverse_baseline'), value.get('reverse_dirty', False),
                value.get('reverse_header_dirty', set()), value.get('reverse_entries', {}),
            )
        discharge = None
        if value.get('discharge') is not None:
            discharge = DischargeWorkspaceState(
                value['discharge'], value.get('discharge_baseline'), value.get('discharge_dirty', set()),
                value.get('discharge_identity_dirty', set()), value.get('discharge_entries', {}),
                value.get('discharge_text', {}),
            )
        value = WorkspaceDraft(value['kind'], value['document'], mdrk, reverse, discharge)
    if not isinstance(value, WorkspaceDraft):
        raise ValueError('Неверный формат рабочего черновика')
    value.validate(folder)
    return value
