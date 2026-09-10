from mdrk_builder.ui.date_rebuild import DateRebuildQueue, RebuildRequest


class EventLoop:
    def __init__(self):
        self.calls = {}
        self.sequence = 0

    def after(self, delay, callback):
        self.sequence += 1
        self.calls[self.sequence] = callback
        return self.sequence

    def after_cancel(self, key):
        self.calls.pop(key, None)

    def step(self):
        self.calls.pop(next(iter(self.calls)))()


def setup_queue():
    loop, jobs, errors = EventLoop(), [], []
    queue = DateRebuildQueue(loop, busy=lambda: False,
        start=lambda scan, finish: jobs.append((scan, finish)), failed=errors.append)
    return queue, loop, jobs, errors


def test_superseded_scan_never_replaces_manual_work_and_save_waits_for_latest():
    queue, loop, jobs, _ = setup_queue()
    applied, saved = [], []
    queue.request("discharge", RebuildRequest("old", lambda: "old", applied.append))
    loop.step()
    queue.request("discharge", RebuildRequest("new", lambda: "new", applied.append))
    assert queue.defer(lambda: saved.append("saved"))
    jobs[0][1]("old", None)
    assert applied == saved == []
    loop.step()
    jobs[1][1]("new", None)
    loop.step()
    assert applied == ["new"] and saved == ["saved"]


def test_incomplete_date_or_patient_switch_discards_inflight_result():
    for cancel in (lambda q: q.request("mdrk", None), lambda q: q.clear()):
        queue, loop, jobs, _ = setup_queue()
        applied = []
        queue.request("mdrk", RebuildRequest("period", lambda: 1, applied.append))
        loop.step()
        cancel(queue)
        jobs[0][1](1, None)
        loop.step()
        assert applied == []


def test_failed_rebuild_retains_retry_and_does_not_export_old_period():
    queue, loop, jobs, errors = setup_queue()
    saved = []
    queue.request("reverse", RebuildRequest("period", lambda: 1, lambda _: None))
    loop.step()
    queue.defer(lambda: saved.append(True))
    jobs[0][1](None, OSError("conversion failed"))
    assert saved == [] and errors and not loop.calls
    assert queue.defer(lambda: saved.append(True))
    loop.step()
    jobs[1][1](1, None)
    loop.step()
    assert saved == [True]
