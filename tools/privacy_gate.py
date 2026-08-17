from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_TEMPLATE = Path("src/mdrk_builder/resources/canonical_mdrk_template.docx")
DISCHARGE_SUMMARY_TEMPLATE = Path(
    "src/mdrk_builder/resources/discharge_summary_template.docx"
)
RUNTIME_TEMPLATES = {CANONICAL_TEMPLATE, DISCHARGE_SUMMARY_TEMPLATE}
ALLOWED_STAFF_NAMES = ("Поляев Б.Б.",)
ALLOWED_ORGANIZATION_NAMES = ("ФГБУ «ФЦМН» ФМБА РОССИИ",)
APPROVED_DISCHARGE_MEDIA = {
    "word/media/image1.png": "bf790a517fad5ffcf3cf05043b284aee5a075ffd47dff6b87a90c0e8f6515434",
    "word/media/image2.png": "146c9f8747d25dbd7b34d63dd4ec9e3adc21ce800bb73d71d800b329dee276e9",
}

EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    ".venv-win",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}
PATIENT_SOURCE_SUFFIXES = {
    ".csv",
    ".doc",
    ".docm",
    ".docx",
    ".heic",
    ".jpeg",
    ".jpg",
    ".log",
    ".odt",
    ".pdf",
    ".png",
    ".rtf",
    ".tif",
    ".tiff",
    ".tsv",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".zip",
}
TEXT_SUFFIXES = {
    "",
    ".bat",
    ".cfg",
    ".ini",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".spec",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_DOCX_PARTS = (
    "customxml/",
    "docprops/custom.xml",
    "docprops/thumbnail.",
    "word/activex/",
    "word/comments",
    "word/embeddings/",
    "word/media/",
    "word/people.xml",
    "word/vbaproject.bin",
)
FORBIDDEN_OOXML_ELEMENTS = {
    "commentRangeEnd",
    "commentRangeStart",
    "commentReference",
    "del",
    "ins",
    "moveFrom",
    "moveTo",
    "trackRevisions",
    "vanish",
    "webHidden",
}


@dataclass(frozen=True)
class Finding:
    path: Path
    reason: str
    line: int | None = None

    def display(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else str(self.path)
        return f"{location}: {self.reason}"


def _risky_text_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    unix_user_path = "/" + "Users" + r"/[^/\s\"']+"
    linux_user_path = "/" + "home" + r"/[^/\s\"']+"
    windows_user_path = r"[A-Za-z]:\\" + "Users" + r"\\[^\\\s\"']+"
    organization_tokens = (
        "\u0413" + "\u0411\u0423\u0417",
        "\u0424" + "\u0413\u0411\u0423",
        "\u0424" + "\u0413\u0411\u041d\u0423",
        "\u041d" + "\u041c\u0418\u0426",
    )
    return (
        (
            "локальный абсолютный путь пользователя",
            re.compile(
                rf"(?:{unix_user_path}|{linux_user_path}|{windows_user_path})",
                re.IGNORECASE,
            ),
        ),
        (
            "реалистичное ФИО после идентифицирующей метки",
            re.compile(
                r"(?:ФИО(?:\s+пациента)?|Пациент(?:ка)?|Фамилия,\s*имя,\s*отчество)\s*:?\s*"
                r"[А-ЯЁ][а-яё-]{2,}\s+[А-ЯЁ][а-яё-]{2,}\s+[А-ЯЁ][а-яё-]{2,}"
            ),
        ),
        (
            "реалистичная фамилия с инициалами",
            re.compile(r"\b[А-ЯЁ][а-яё-]{2,}\s+[А-ЯЁ]\.[А-ЯЁ]\."),
        ),
        (
            "идентификатор медицинской организации",
            re.compile(
                rf"\b(?:{'|'.join(map(re.escape, organization_tokens))})\b",
                re.IGNORECASE,
            ),
        ),
    )


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in path.parts)


def _relative(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return path


def _source_paths(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=True,
            capture_output=True,
        )
        relative_paths = [Path(value) for value in result.stdout.decode().split("\0") if value]
        paths = [root / path for path in relative_paths]
    except (FileNotFoundError, subprocess.CalledProcessError, UnicodeDecodeError):
        paths = [path for path in root.rglob("*") if path.is_file()]
    return sorted(path for path in paths if path.is_file() and not _is_excluded(_relative(path, root)))


def _read_text(path: Path) -> str | None:
    if path.suffix.casefold() not in TEXT_SUFFIXES:
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _without_allowed_text(text: str) -> str:
    for full_name in ALLOWED_STAFF_NAMES:
        text = text.replace(full_name, "РАЗРЕШЕННЫЙ_СОТРУДНИК")
    for organization_name in ALLOWED_ORGANIZATION_NAMES:
        text = text.replace(organization_name, "РАЗРЕШЕННАЯ_ОРГАНИЗАЦИЯ")
    return text


def _scan_text(path: Path, display_path: Path) -> list[Finding]:
    text = _read_text(path)
    if text is None:
        return []
    text = _without_allowed_text(text)
    findings: list[Finding] = []
    for reason, pattern in _risky_text_patterns():
        for match in pattern.finditer(text):
            findings.append(Finding(display_path, reason, text.count("\n", 0, match.start()) + 1))
    return findings


def _scan_binary_markers(path: Path, display_path: Path) -> list[Finding]:
    data = path.read_bytes()
    findings: list[Finding] = []
    markers = (
        "/" + "Users" + "/",
        "C:\\" + "Users" + "\\",
    )
    for marker in markers:
        if marker.encode("utf-8") in data or marker.encode("utf-16-le") in data:
            findings.append(Finding(display_path, f"бинарный файл содержит рискованный маркер: {marker}"))
    return findings


def audit_docx(path: Path, display_path: Path | None = None) -> list[Finding]:
    shown = display_path or path
    is_discharge_template = (
        shown == DISCHARGE_SUMMARY_TEMPLATE
        or path.resolve() == (PROJECT_ROOT / DISCHARGE_SUMMARY_TEMPLATE).resolve()
    )
    findings: list[Finding] = []
    try:
        with ZipFile(path) as archive:
            corrupt = archive.testzip()
            if corrupt:
                return [Finding(shown, f"повреждённая часть DOCX: {corrupt}")]
            names = archive.namelist()
            lowered = [name.casefold() for name in names]
            for name, low_name in zip(names, lowered, strict=True):
                if is_discharge_template and name in APPROVED_DISCHARGE_MEDIA:
                    digest = hashlib.sha256(archive.read(name)).hexdigest()
                    if digest != APPROVED_DISCHARGE_MEDIA[name]:
                        findings.append(
                            Finding(shown, f"изменён утверждённый медиа-ресурс DOCX: {name}")
                        )
                    continue
                if any(low_name.startswith(prefix) for prefix in FORBIDDEN_DOCX_PARTS):
                    findings.append(Finding(shown, f"запрещённая скрытая часть DOCX: {name}"))
            if is_discharge_template:
                missing_media = set(APPROVED_DISCHARGE_MEDIA).difference(names)
                for name in sorted(missing_media):
                    findings.append(Finding(shown, f"отсутствует утверждённый медиа-ресурс DOCX: {name}"))
            for name in names:
                if not name.casefold().endswith((".xml", ".rels")):
                    continue
                data = archive.read(name)
                try:
                    root = ElementTree.fromstring(data)
                except ElementTree.ParseError:
                    findings.append(Finding(shown, f"невалидный OOXML: {name}"))
                    continue
                for element in root.iter():
                    local_name = element.tag.rsplit("}", 1)[-1]
                    if local_name in FORBIDDEN_OOXML_ELEMENTS:
                        findings.append(Finding(shown, f"скрытая/редакционная разметка {local_name} в {name}"))
                    if local_name in {"rsid", "rsidRoot", "rsids"}:
                        findings.append(Finding(shown, f"идентификатор сессии редактирования в {name}"))
                    for attribute in element.attrib:
                        if attribute.rsplit("}", 1)[-1].casefold().startswith("rsid"):
                            findings.append(Finding(shown, f"атрибут сессии редактирования в {name}"))
                decoded = _without_allowed_text(data.decode("utf-8", errors="ignore"))
                for reason, pattern in _risky_text_patterns():
                    if pattern.search(decoded):
                        findings.append(Finding(shown, f"{reason} внутри {name}"))
            core_name = next((name for name in names if name.casefold() == "docprops/core.xml"), None)
            if core_name:
                core = ElementTree.fromstring(archive.read(core_name))
                for element in core.iter():
                    local_name = element.tag.rsplit("}", 1)[-1]
                    if local_name in {"creator", "lastModifiedBy"} and (element.text or "") not in {
                        "",
                        "MDRK Builder",
                    }:
                        findings.append(Finding(shown, f"персональный автор в {core_name}"))
    except (BadZipFile, OSError) as error:
        findings.append(Finding(shown, f"DOCX не читается: {error}"))
    return _deduplicate(findings)


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    return list(dict.fromkeys(findings))


def audit_source_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _source_paths(root):
        relative = _relative(path, root)
        suffix = path.suffix.casefold()
        if suffix in PATIENT_SOURCE_SUFFIXES:
            if relative in RUNTIME_TEMPLATES:
                findings.extend(audit_docx(path, relative))
            else:
                findings.append(Finding(relative, "patient/source формат запрещён в исходном дереве"))
            continue
        findings.extend(_scan_text(path, relative))
    return _deduplicate(findings)


def _candidate_files(candidate: Path) -> list[Path]:
    if candidate.is_file():
        return [candidate]
    if candidate.is_dir():
        return sorted(path for path in candidate.rglob("*") if path.is_file())
    return []


def audit_release_candidate(candidate: Path) -> list[Finding]:
    if not candidate.exists():
        return [Finding(candidate, "release candidate не найден")]
    findings: list[Finding] = []
    for path in _candidate_files(candidate):
        shown = _relative(path, candidate.parent if candidate.is_file() else candidate)
        if path.name.casefold() == "issues.txt":
            # The deployed pilot file is intentionally mutable and may contain
            # contact details entered by clinicians. It is empty in a fresh package.
            continue
        if path.suffix.casefold() in PATIENT_SOURCE_SUFFIXES:
            findings.append(Finding(shown, "patient/source формат запрещён в комплекте поставки"))
            continue
        text_findings = _scan_text(path, shown)
        findings.extend(text_findings or _scan_binary_markers(path, shown))
    return _deduplicate(findings)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed privacy audit for MDRK Builder source and internal release files."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--release-candidate",
        type=Path,
        action="append",
        default=[],
        help="EXE or distribution folder to audit; may be repeated.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    findings = audit_source_tree(root)
    for candidate in args.release_candidate:
        findings.extend(audit_release_candidate(candidate.resolve()))
    findings = _deduplicate(findings)
    if findings:
        print("PRIVACY GATE FAILED", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.display()}", file=sys.stderr)
        return 1
    print("PRIVACY GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
