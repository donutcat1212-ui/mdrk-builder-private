"""Bounded undo for structured edits, separate from each text widget's own history."""
from copy import deepcopy
from functools import wraps


class EditHistory:
    def __init__(self, capture, restore, on_change=None):
        self.capture, self.restore = capture, restore
        self.past, self.future = [], []
        self.on_change = on_change
        self._editing = False

    def wrap(self, operation):
        @wraps(operation)
        def run(*args, **kwargs):
            if self._editing:
                return operation(*args, **kwargs)
            before = deepcopy(self.capture())
            self._editing = True
            try:
                result = operation(*args, **kwargs)
                after = self.capture()
                if before != after:
                    if self.on_change is not None:
                        self.on_change(before, after)
                    self.past.append(before)
                    self.past = self.past[-30:]
                    self.future.clear()
                return result
            finally:
                self._editing = False
        return run

    def undo(self):
        if self.past:
            self.future.append(deepcopy(self.capture()))
            self.restore(self.past.pop())

    def redo(self):
        if self.future:
            self.past.append(deepcopy(self.capture()))
            self.restore(self.future.pop())

    def clear(self):
        self.past.clear(); self.future.clear()
