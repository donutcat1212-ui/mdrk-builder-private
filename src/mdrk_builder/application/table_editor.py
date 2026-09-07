"""One transaction history for facts shared by MDRK and discharge documents."""
from copy import deepcopy
from dataclasses import dataclass, fields

from mdrk_builder.application.edit_history import EditHistory
from mdrk_builder.application.shared_edits import transfer_discharge_edits, transfer_episode_edits
from mdrk_builder.domain import (
    DischargeScaleRow, DischargeTeamFinding, IcfDomain, Procedure, SpecialistFinding,
)


@dataclass
class EpisodeTables:
    icf_domains: list[IcfDomain]
    procedures: list[Procedure]
    findings: list[SpecialistFinding]


@dataclass
class DischargeTables:
    icf_domains: tuple[IcfDomain, ...]
    completed_procedures: tuple[Procedure, ...]
    team_findings: tuple[DischargeTeamFinding, ...]
    admission_scale_rows: tuple[DischargeScaleRow, ...]
    discharge_scale_rows: tuple[DischargeScaleRow, ...]


@dataclass
class TableState:
    episode: EpisodeTables | None
    discharge: DischargeTables | None


def _capture(model, state_type):
    if model is None:
        return None
    return state_type(**{field.name: deepcopy(getattr(model, field.name)) for field in fields(state_type)})


def _restore(model, state):
    if model is not None and state is not None:
        for field in fields(state):
            setattr(model, field.name, deepcopy(getattr(state, field.name)))


class SharedTableEditor:
    """Panels supply models and redraw callbacks; reconciliation happens only on edit."""
    def __init__(self, *, episode, discharge, refresh_episode, refresh_discharge):
        self.episode, self.discharge = episode, discharge
        self.refresh_episode, self.refresh_discharge = refresh_episode, refresh_discharge
        self.history = EditHistory(self.capture, self.restore, self._edited)

    def capture(self):
        return TableState(_capture(self.episode(), EpisodeTables), _capture(self.discharge(), DischargeTables))

    def episode_loaded(self, discharge_baseline):
        """Apply edits made in discharge before the first MDRK scan, or a rescan."""
        self.history.clear()
        return transfer_discharge_edits(self.discharge(), self.episode(), baseline=discharge_baseline)

    def restore(self, state):
        _restore(self.episode(), state.episode)
        _restore(self.discharge(), state.discharge)
        self.refresh_episode()
        self.refresh_discharge()

    def _edited(self, before, after):
        if before.episode != after.episode:
            transfer_episode_edits(self.episode(), before.episode, self.discharge())
            self.refresh_discharge()
        elif before.discharge != after.discharge:
            transfer_discharge_edits(self.discharge(), self.episode(), baseline=before.discharge)
            self.refresh_episode()
