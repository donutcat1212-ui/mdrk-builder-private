"""Bounded undo for structured edits, separate from each text widget's own history."""
from copy import deepcopy
from functools import wraps


class EditHistory:
    def __init__(self, capture, restore):
        self.capture, self.restore = capture, restore
        self.past, self.future = [], []

    def wrap(self, operation):
        @wraps(operation)
        def run(*args, **kwargs):
            before = deepcopy(self.capture())
            result = operation(*args, **kwargs)
            if before != self.capture():
                self.past.append(before)
                self.past = self.past[-30:]
                self.future.clear()
            return result
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


def install_history(owner, names, capture, restore):
    history = EditHistory(capture, restore)
    for name in names:
        setattr(owner, name, history.wrap(getattr(owner, name)))
    return history
