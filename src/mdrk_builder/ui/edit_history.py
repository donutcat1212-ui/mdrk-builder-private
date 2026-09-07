"""Bind UI operations to a supplied document editing history."""
from mdrk_builder.application.edit_history import EditHistory


def install_history(owner, names, capture=None, restore=None, *, history=None):
    if history is None:
        history = EditHistory(capture, restore)
    for name in names:
        setattr(owner, name, history.wrap(getattr(owner, name)))
    return history
