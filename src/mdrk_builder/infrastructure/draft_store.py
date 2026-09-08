"""Versioned local JSON drafts. Decode only known domain types, never executable objects."""
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
import hashlib
import sys
from pathlib import Path
import os
from tempfile import NamedTemporaryFile
from mdrk_builder import domain
from mdrk_builder.application.workspace import (WorkspaceDraft, MdrkWorkspaceState, ReverseWorkspaceState, DischargeWorkspaceState)

TYPES = {name: getattr(domain, name) for name in domain.__all__ if isinstance(getattr(domain, name), type)}

TYPES.update({cls.__name__: cls for cls in (WorkspaceDraft, MdrkWorkspaceState, ReverseWorkspaceState, DischargeWorkspaceState)})


def _workspace_draft_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "MDRK Builder" / "drafts"


def workspace_draft_path(folder: Path) -> Path:
    """Per-user local storage; episode folders remain input-only for autosave."""
    key = os.path.normcase(str(Path(folder).resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _workspace_draft_directory() / (digest + ".json")


def import_legacy_workspace_draft(folder: Path) -> Path:
    """Move an old managed draft only after validating and reopening its local copy."""
    from mdrk_builder.application.workspace import restore_workspace_state
    path = workspace_draft_path(folder)
    legacy = folder / ".mdrk draft.json"
    if path.exists() or not legacy.is_file():
        return path
    state = restore_workspace_state(load_draft(legacy), folder)
    save_draft(path, state)
    if load_draft(path) != state:
        raise OSError("Не удалось проверить перенос рабочего черновика")
    legacy.unlink()
    return path


def encode(value):
    if isinstance(value, Enum):
        return {'type': type(value).__name__, 'enum': value.value}
    if is_dataclass(value):
        return {'type': type(value).__name__, 'fields': {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, (datetime, date, Path)):
        return {'type': 'datetime' if isinstance(value, datetime) else 'date' if isinstance(value, date) else 'path', 'value': str(value)}
    if isinstance(value, (tuple, set)):
        return {'type': type(value).__name__, 'items': [encode(v) for v in value]}
    if isinstance(value, dict):
        return {'type': 'dict', 'items': [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'Unsupported draft value: {type(value).__name__}')


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get('type')
    if kind == 'path': return Path(value['value'])
    if kind == 'date': return date.fromisoformat(value['value'])
    if kind == 'datetime': return datetime.fromisoformat(value['value'])
    if kind == 'tuple': return tuple(decode(v) for v in value['items'])
    if kind == 'set': return set(decode(v) for v in value['items'])
    if kind == 'dict': return {decode(k): decode(v) for k, v in value['items']}
    cls = TYPES.get(kind)
    if cls is None: raise ValueError('Неизвестный тип в черновике')
    if issubclass(cls, Enum): return cls(value['enum'])
    return cls(**{k: decode(v) for k, v in value['fields'].items()})


def save_draft(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
            name = f.name
            json.dump({'version': 1, 'state': encode(state)}, f, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if name and Path(name).exists(): Path(name).unlink()


def load_draft(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('version') != 1: raise ValueError('Неподдерживаемая версия черновика')
    return decode(data['state'])
