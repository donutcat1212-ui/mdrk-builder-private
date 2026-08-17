#!/usr/bin/env python3
"""Create the minimal copy-ready MDRK Builder internal distribution folder."""

from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
PACKAGE_VERSION_PATTERN = re.compile(
    r'^__version__\s*=\s*["\'](?P<version>[^"\']+)["\']\s*$',
    flags=re.MULTILINE,
)
ALLOWED_PACKAGE_FILES = {
    "MDRK_Builder.exe",
    "issues.txt",
    "README_ПЕРЕД_ИСПОЛЬЗОВАНИЕМ.txt",
}


def read_project_version(project_root: Path) -> str:
    """Return the version only when package and project metadata agree."""

    pyproject_path = project_root / "pyproject.toml"
    package_init_path = project_root / "src" / "mdrk_builder" / "__init__.py"

    with pyproject_path.open("rb") as stream:
        project_version = str(tomllib.load(stream)["project"]["version"])

    package_init = package_init_path.read_text(encoding="utf-8")
    match = PACKAGE_VERSION_PATTERN.search(package_init)
    if match is None:
        raise RuntimeError(f"Package version is missing: {package_init_path}")
    package_version = match.group("version")

    if project_version != package_version:
        raise RuntimeError(
            "Version mismatch: "
            f"pyproject.toml={project_version}, mdrk_builder.__version__={package_version}"
        )
    if SEMVER_PATTERN.fullmatch(package_version) is None:
        raise RuntimeError(f"Internal release version must be X.Y.Z: {package_version}")
    return package_version


def read_internal_use_text(project_root: Path, version: str) -> str:
    template_path = project_root / "INTERNAL_USE_RU.txt"
    template = template_path.read_text(encoding="utf-8")
    placeholder = "{{VERSION}}"
    if template.count(placeholder) != 1:
        raise RuntimeError(
            f"Internal-use template must contain {placeholder} exactly once: {template_path}"
        )
    return template.replace(placeholder, version)


def build_internal_package(
    *,
    project_root: Path,
    executable: Path,
    dist_dir: Path,
    replace: bool,
) -> Path:
    """Build and validate a three-file internal distribution directory."""

    project_root = project_root.resolve()
    executable = executable.resolve()
    dist_dir = dist_dir.resolve()
    version = read_project_version(project_root)

    if not executable.is_file() or executable.suffix.lower() != ".exe":
        raise FileNotFoundError(f"Built Windows executable is missing: {executable}")

    dist_dir.mkdir(parents=True, exist_ok=True)
    package_name = f"MDRK_Builder_{version}_Internal"
    package_dir = dist_dir / package_name
    if package_dir.exists() and not replace:
        raise FileExistsError(
            f"Internal package already exists: {package_dir}. Use --replace to rebuild it."
        )

    staging_dir = Path(tempfile.mkdtemp(prefix=f".{package_name}-", dir=dist_dir))
    try:
        packaged_executable = staging_dir / "MDRK_Builder.exe"
        readme_path = staging_dir / "README_ПЕРЕД_ИСПОЛЬЗОВАНИЕМ.txt"
        issues_path = staging_dir / "issues.txt"

        shutil.copy2(executable, packaged_executable)
        readme_path.write_text(
            read_internal_use_text(project_root, version),
            encoding="utf-8-sig",
            newline="\r\n",
        )
        issues_path.write_bytes(b"\xef\xbb\xbf")

        actual_files = {path.name for path in staging_dir.iterdir() if path.is_file()}
        if actual_files != ALLOWED_PACKAGE_FILES:
            raise RuntimeError(
                "Unexpected internal package contents: "
                f"expected={sorted(ALLOWED_PACKAGE_FILES)}, actual={sorted(actual_files)}"
            )
        if any(path.is_dir() for path in staging_dir.iterdir()):
            raise RuntimeError("Internal package must not contain subdirectories")

        if package_dir.exists():
            if package_dir.parent != dist_dir or package_dir.name != package_name:
                raise RuntimeError(f"Refusing to replace unexpected path: {package_dir}")
            shutil.rmtree(package_dir)
        staging_dir.rename(package_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return package_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the minimal internal MDRK Builder Windows package."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Project root containing pyproject.toml (default: detected root).",
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=PROJECT_ROOT / "dist" / "MDRK_Builder.exe",
        help="Path to the PyInstaller-built Windows executable.",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=PROJECT_ROOT / "dist",
        help="Parent directory for MDRK_Builder_X.Y.Z_Internal.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace the exact same-version package directory after staging succeeds.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package_dir = build_internal_package(
        project_root=args.project_root,
        executable=args.exe,
        dist_dir=args.dist_dir,
        replace=args.replace,
    )
    print(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
