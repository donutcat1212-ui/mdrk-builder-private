"""Translate document date controls into background rebuilds of their source data."""
from copy import deepcopy
from datetime import datetime, time
from tkinter import messagebox

from mdrk_builder.application.discharge_summary import scan_discharge_summary
from mdrk_builder.application.reverse_sheet import scan_reverse_sheet
from mdrk_builder.application.scanner import scan_patient_folder
from mdrk_builder.domain import MdrkKind
from mdrk_builder.domain.document_dates import discharge_document_datetime
from mdrk_builder.ui.date_rebuild import DateRebuildQueue, RebuildRequest
from mdrk_builder.ui.episode_adapter import parse_optional_datetime


def _day(value):
    return value.date() if value else None


class DocumentDateRebuilds:
    def __init__(self, app):
        self.app = app
        self.queue = DateRebuildQueue(
            app.root, busy=lambda: app._scanning,
            start=self._start, failed=self._failed,
        )

    def _start(self, operation, finished):
        app = self.app
        app._set_scanning(True)
        app.status_var.set("Пересбор данных по выбранным датам… Ручные правки сохраняются.")

        def deliver(value, error):
            app._set_scanning(False)
            finished(value, error)

        app._start_background_job(operation, deliver, thread_name="document-date-rebuild")

    def _failed(self, error):
        self.app.status_var.set("Не удалось обновить данные по датам. Ручные правки сохранены.")
        messagebox.showerror("Ошибка пересбора", str(error), parent=self.app.root)

    def changed(self, document):
        key = "mdrk" if document.startswith("mdrk") else document
        try:
            request = self._mdrk() if key == "mdrk" else self._auxiliary(key)
        except ValueError:
            request = None  # Incomplete date while typing also invalidates the old job.
        self.queue.request(key, request)

    def _mdrk(self):
        app = self.app
        episode = app.episode
        if episode is None:
            return None
        admission = parse_optional_datetime(app._entry_variables["admission"].get())
        meeting = parse_optional_datetime(app._entry_variables["meeting"].get())
        if admission is None or meeting is None:
            raise ValueError("Incomplete period")
        meetings = {kind: episode.meeting_at(kind) for kind in MdrkKind}
        meetings[app._current_kind] = meeting
        signature = (_day(admission), *(_day(meetings[kind]) for kind in MdrkKind))
        baseline = (_day(episode.materialized_admission_datetime or episode.admission_datetime),
                    *(_day(episode.assessment_at(kind)) for kind in MdrkKind))
        if signature == baseline:
            return None
        final_date_changed = _day(meetings[MdrkKind.FINAL]) != _day(episode.assessment_at(MdrkKind.FINAL))
        overrides = {"admission_datetime_override": admission,
                     "medical_record_number_override": app._entry_variables["record_number"].get().strip()}
        for kind, name in ((MdrkKind.INITIAL, "initial_meeting_at"), (MdrkKind.FINAL, "final_meeting_at")):
            value = meetings[kind]
            if value is not None:
                overrides[name] = (datetime.combine(value.date(), time.max)
                                   if _day(value) != _day(episode.assessment_at(kind))
                                   else episode.assessment_at(kind))
        folder = episode.folder

        def apply(result):
            if not app._folder_field_matches(folder):
                return
            # Capture at delivery, so edits made during conversion are also retained.
            app._pending_manual_state = app._capture_manual_state()
            baseline = deepcopy(result)
            app._merge_manual_state(result)
            if final_date_changed:
                result.course_end_override = meetings[MdrkKind.FINAL]
                from mdrk_builder.application.scanner import _update_course_duration
                _update_course_duration(result)
            app.episode, app._scan_baseline = result, baseline
            app._populate_from_episode()
            app._update_action_states()
            app.status_var.set("Данные МДРК обновлены по датам. Ручные правки сохранены.")

        return RebuildRequest(signature,
            lambda: scan_patient_folder(folder, scan_session=app._scan_session, **overrides), apply)

    def _auxiliary(self, key):
        app = self.app
        panel = app.discharge_workspace if key == "discharge" else app.reverse_workspace
        draft = panel.draft
        if draft is None:
            return None
        variables = panel._identity_vars if key == "discharge" else panel._header_vars
        admission = parse_optional_datetime(variables["admission"].get())
        discharge = parse_optional_datetime(variables["discharge"].get())
        if key == "discharge":
            discharge = discharge_document_datetime(discharge)
        if admission is None or (key == "discharge" and discharge is None):
            raise ValueError("Incomplete period")
        if discharge and admission.date() > discharge.date():
            raise ValueError("Reversed period")
        baseline = panel.source_baseline if key == "discharge" else panel._baseline
        signature = (admission, discharge)
        baseline_discharge = baseline.discharge_datetime if baseline else None
        if key == "discharge":
            baseline_discharge = discharge_document_datetime(baseline_discharge)
        if baseline and signature == (baseline.admission_datetime, baseline_discharge):
            return None
        folder = draft.folder
        scan = scan_discharge_summary if key == "discharge" else scan_reverse_sheet

        def apply(result):
            if not app._folder_field_matches(folder):
                return
            if panel.merge_scan(result) is False:
                return
            if key == "discharge":
                app.discharge_draft = panel.draft
            else:
                app.reverse_draft = panel.draft
            app._update_action_states()
            app.status_var.set("Данные документа обновлены по датам. Ручные правки сохранены.")

        return RebuildRequest(signature, lambda: scan(folder, scan_session=app._scan_session,
            admission_datetime_override=admission, discharge_datetime_override=discharge), apply)

    def defer_save(self):
        app = self.app
        document = app.document_var.get()
        if self.queue.active is None and not self.queue.pending:
            self.changed(document)
        return self.queue.defer(lambda: app._generate() if app.document_var.get() == document else None)
