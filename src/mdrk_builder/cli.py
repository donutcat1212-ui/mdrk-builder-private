from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mdrk_builder.application.scanner import scan_patient_folder
from mdrk_builder.application.validation import can_generate, current_issues
from mdrk_builder.domain import Episode, MdrkKind, ReviewSeverity
from mdrk_builder.infrastructure.docx_writer import write_mdrk_docx
from mdrk_builder.ui.episode_adapter import (
    format_date,
    format_datetime,
    parse_optional_meeting_datetime,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdrk-scan",
        description="Локально просканировать папку эпизода и при необходимости создать МДРК DOCX.",
    )
    parser.add_argument("folder", type=Path, help="папка одного эпизода реабилитации")
    parser.add_argument(
        "--kind",
        choices=[kind.value for kind in MdrkKind],
        default=MdrkKind.INITIAL.value,
        help="initial = МДРК-1, final = МДРК-2",
    )
    parser.add_argument(
        "--meeting",
        metavar='"ДД.ММ.ГГГГ ЧЧ:ММ"',
        help="ручная дата и время выбранного заседания",
    )
    parser.add_argument("--output", type=Path, help="создать DOCX по указанному пути")
    parser.add_argument(
        "--force",
        action="store_true",
        help="разрешить замену уже существующего --output",
    )
    parser.add_argument("--json", action="store_true", help="вывести машинно-читаемую сводку")
    return parser


def _summary(episode: Episode, kind: MdrkKind) -> dict[str, object]:
    issues = current_issues(episode, kind)
    return {
        "folder": str(episode.folder),
        "kind": kind.value,
        "meeting_at": format_datetime(episode.meeting_at(kind)),
        "patient": {
            "full_name": episode.identity.full_name,
            "birth_date": format_date(episode.identity.birth_date),
            "sex": episode.identity.sex,
            "medical_record_number": episode.identity.medical_record_number,
        },
        "admission_at": format_datetime(episode.admission_datetime),
        "sources": len(episode.sources),
        "findings": len(episode.findings),
        "icf_domains": len(episode.icf_domains),
        "procedures": len(episode.procedures),
        "can_generate": can_generate(episode, kind),
        "issues": [
            {
                "severity": issue.severity.value,
                "code": issue.code,
                "field": issue.field,
                "message": issue.message,
                "source": str(issue.source) if issue.source else "",
            }
            for issue in issues
        ],
    }


def _print_human_summary(episode: Episode, kind: MdrkKind) -> None:
    print(f"Папка: {episode.folder}")
    print(f"Снимок: {'МДРК-1' if kind is MdrkKind.INITIAL else 'МДРК-2'}")
    print(f"Заседание: {format_datetime(episode.meeting_at(kind)) or 'не определено'}")
    print(f"Пациент: {episode.identity.full_name or 'не определён'}")
    print(f"Номер ИБ: {episode.identity.medical_record_number or 'не определён'}")
    print(
        "Найдено: "
        f"источников {len(episode.sources)}, заключений {len(episode.findings)}, "
        f"доменов МКФ {len(episode.icf_domains)}, процедур {len(episode.procedures)}"
    )
    issues = current_issues(episode, kind)
    if not issues:
        print("Предупреждений нет.")
        return
    print("Предупреждения:")
    for issue in issues:
        prefix = {
            ReviewSeverity.BLOCKING: "БЛОК",
            ReviewSeverity.WARNING: "ВНИМАНИЕ",
            ReviewSeverity.INFO: "ИНФО",
        }[issue.severity]
        print(f"  [{prefix}] {issue.message}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kind = MdrkKind(args.kind)
    if args.output and args.output.exists() and not args.force:
        print(
            f"Ошибка: файл уже существует: {args.output}. "
            "Для замены укажите --force.",
            file=sys.stderr,
        )
        return 1
    try:
        meeting = (
            parse_optional_meeting_datetime(args.meeting)
            if args.meeting
            else None
        )
        if args.meeting:
            if meeting is None:
                raise ValueError("Дата заседания не может быть пустой")
        meeting_override = (
            {"initial_meeting_at": meeting}
            if meeting is not None and kind is MdrkKind.INITIAL
            else {"final_meeting_at": meeting}
            if meeting is not None
            else {}
        )
        episode = scan_patient_folder(args.folder, **meeting_override)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(_summary(episode, kind), ensure_ascii=False, indent=2))
    else:
        _print_human_summary(episode, kind)

    if args.output:
        if not can_generate(episode, kind):
            print("DOCX не создан: устраните блокирующие проблемы.", file=sys.stderr)
            return 2
        try:
            created = write_mdrk_docx(episode, kind, args.output)
        except Exception as exc:
            print(f"Не удалось создать DOCX: {exc}", file=sys.stderr)
            return 1
        print(f"DOCX создан: {created}", file=sys.stderr if args.json else sys.stdout)
    return 0 if can_generate(episode, kind) else 2


if __name__ == "__main__":
    raise SystemExit(main())
