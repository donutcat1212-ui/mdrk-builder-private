"""Debounce date edits, serialize rescans, and reject superseded results."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class RebuildRequest:
    signature: object
    scan: Callable
    apply: Callable


class DateRebuildQueue:
    def __init__(self, root, *, busy, start, failed, delay_ms=650):
        self.root, self.busy, self.start, self.failed = root, busy, start, failed
        self.delay_ms = delay_ms
        self.pending = {}
        self.revisions = {}
        self.active = None
        self.timer = None
        self.after_ready = None

    def request(self, key, request):
        self.revisions[key] = self.revisions.get(key, 0) + 1
        self.pending.pop(key, None)
        if request is not None:
            self.pending[key] = request
        self._schedule(self.delay_ms)

    def clear(self):
        for key in self.revisions:
            self.revisions[key] += 1
        self.pending.clear()
        self.after_ready = None

    def defer(self, callback):
        if not self.pending and self.active is None:
            return False
        self.after_ready = callback
        self._schedule(0)
        return True

    def _schedule(self, delay):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.timer = self.root.after(delay, self._pump)

    def _pump(self):
        self.timer = None
        if self.active is not None or self.busy():
            self._schedule(100)
            return
        if not self.pending:
            callback, self.after_ready = self.after_ready, None
            if callback:
                callback()
            return
        key = next(iter(self.pending))
        request = self.pending.pop(key)
        revision = self.revisions[key]
        self.active = key

        def finished(value, error):
            self.active = None
            if revision == self.revisions[key]:
                if error is not None:
                    # A failed scan cannot silently unlock export of the old period.
                    self.after_ready = None
                    self.pending[key] = request
                    if self.timer is not None:
                        self.root.after_cancel(self.timer)
                        self.timer = None
                    self.failed(error)
                    return
                request.apply(value)
            self._schedule(0)

        self.start(request.scan, finished)
