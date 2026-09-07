"""Versioned local JSON drafts. Decode only known domain types, never executable objects."""
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
from pathlib import Path
import os
from tempfile import NamedTemporaryFile
from mdrk_builder import domain

TYPES = {name: getattr(domain, name) for name in domain.__all__ if isinstance(getattr(domain, name), type)}


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
