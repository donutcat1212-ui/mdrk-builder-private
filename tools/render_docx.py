from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


SOFFICE_CANDIDATES = (
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    Path("C:/Program Files/LibreOffice/program/soffice.exe"),
)


def _find_executable(name: str, candidates: tuple[Path, ...] = ()) -> Path:
    discovered = shutil.which(name)
    if discovered:
        return Path(discovered)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Не найден обязательный исполняемый файл: {name}")


def render_docx(source: Path, output_dir: Path, *, dpi: int = 144) -> tuple[Path, ...]:
    """Render a DOCX to a copied PDF and one PNG per page for development QA."""

    source = source.resolve()
    output_dir = output_dir.resolve()
    if not source.is_file() or source.suffix.casefold() != ".docx":
        raise ValueError(f"Ожидался существующий DOCX: {source}")
    if dpi < 72:
        raise ValueError("dpi must be at least 72")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{source.stem}.pdf"
    stale_artifacts = [*output_dir.glob("page-*.png")]
    if output_pdf.exists():
        stale_artifacts.append(output_pdf)
    if stale_artifacts:
        raise FileExistsError(
            f"Каталог render-QA должен быть свежим: {output_dir}"
        )

    soffice = _find_executable("soffice", SOFFICE_CANDIDATES)
    pdftoppm = _find_executable("pdftoppm")

    with tempfile.TemporaryDirectory(prefix="mdrk-render-") as temporary:
        temporary_dir = Path(temporary)
        conversion_dir = temporary_dir / "pdf"
        conversion_dir.mkdir()
        profile_dir = temporary_dir / "lo-profile"
        profile_dir.mkdir()
        subprocess.run(
            (
                str(soffice),
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(conversion_dir),
                str(source),
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        converted_pdf = conversion_dir / f"{source.stem}.pdf"
        if not converted_pdf.is_file():
            raise RuntimeError("LibreOffice завершился без ожидаемого PDF")
        output_pdf = output_dir / converted_pdf.name
        shutil.copy2(converted_pdf, output_pdf)

    page_prefix = output_dir / "page"
    subprocess.run(
        (str(pdftoppm), "-png", "-r", str(dpi), str(output_pdf), str(page_prefix)),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    pages = tuple(sorted(output_dir.glob("page-*.png")))
    if not pages:
        raise RuntimeError("pdftoppm завершился без PNG-страниц")
    return (output_pdf, *pages)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render DOCX pages for MDRK visual QA")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=144)
    args = parser.parse_args()
    for artifact in render_docx(args.source, args.output_dir, dpi=args.dpi):
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
