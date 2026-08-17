# MDRK Builder Project Notes

## Scope

- Local, offline-first Windows desktop tool for assembling editable MDRK-1,
  MDRK-2, discharge-summary and reverse-sheet DOCX files from one rehabilitation
  episode folder.
- Supported inputs: DOCX, DOC and RTF. Ignore PDF.
- Never modify source patient documents.
- Local hand-filled MDRKs may be used only for private validation and are never
  committed or distributed. Their mistakes are not program rules. Current
  episode sources plus `CONTEXT.md` and `docs/acceptance.md` are authoritative.

## Domain invariants

- MDRK-1 and MDRK-2 are initial/final snapshots of one episode model.
- Select the latest document per specialist by clinical date/time inside the
  document and not by filename or filesystem metadata.
- Missing optional specialists are not errors.
- Do not import rows explicitly assigned to another specialist from a physician
  source. Preserve ownerless domains and the episode-level `Pf` description.
- Count completed procedures by date cells containing `+`; do not copy legacy
  MDRK counts.
- Keep uncertain or missing values empty and visible as review issues; never
  invent a plausible clinical value.
- Final goal/tasks use the fixed 95%-case defaults requested by the user; rare
  exceptions are edited in the output DOCX.

## Architecture

- `domain/`: framework-independent episode model.
- `application/`: extraction, scan orchestration and snapshot selection.
- `infrastructure/`: OOXML, Word/LibreOffice conversion and DOCX output.
- `ui/`: Tkinter adapter only; business rules do not live in widgets.
- Production legacy conversion uses installed desktop Microsoft Word through a
  narrow pywin32 COM adapter. LibreOffice is a development adapter on macOS.

## Verification

From this directory:

```bash
PYTHONPATH=src uv run --with 'pytest>=8.3,<10' pytest -q
PYTHONPATH=src python -m compileall -q src tests
```

Render every generated DOCX and inspect every page before delivery. Reference
documents used for that check remain local and outside version control. A
Windows EXE is complete only after it has been built on Windows and smoke-tested
with desktop Word on a clean profile without Python.
