from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "tools" / "package_internal_release.py"
INTERNAL_USE_TEMPLATE = SCRIPT_PATH.parents[1] / "INTERNAL_USE_RU.txt"
SPEC = importlib.util.spec_from_file_location("package_internal_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
package_internal_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_internal_release)


def _write_project_version(project_root: Path, *, project: str, package: str) -> None:
    (project_root / "src" / "mdrk_builder").mkdir(parents=True)
    (project_root / "pyproject.toml").write_text(
        f'[project]\nname = "mdrk-builder"\nversion = "{project}"\n',
        encoding="utf-8",
    )
    (project_root / "src" / "mdrk_builder" / "__init__.py").write_text(
        f'__version__ = "{package}"\n',
        encoding="utf-8",
    )
    (project_root / "INTERNAL_USE_RU.txt").write_text(
        INTERNAL_USE_TEMPLATE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def test_builds_minimal_copy_ready_internal_folder(tmp_path: Path) -> None:
    _write_project_version(tmp_path, project="1.0.0", package="1.0.0")
    source_exe = tmp_path / "MDRK_Builder.exe"
    source_exe.write_bytes(b"minimal-test-executable")

    package_dir = package_internal_release.build_internal_package(
        project_root=tmp_path,
        executable=source_exe,
        dist_dir=tmp_path / "dist",
        replace=False,
    )

    assert package_dir.name == "MDRK_Builder_1.0.0_Internal"
    assert {path.name for path in package_dir.iterdir()} == {
        "MDRK_Builder.exe",
        "issues.txt",
        "README_ПЕРЕД_ИСПОЛЬЗОВАНИЕМ.txt",
    }
    assert (package_dir / "MDRK_Builder.exe").read_bytes() == source_exe.read_bytes()
    assert (package_dir / "issues.txt").read_bytes() == b"\xef\xbb\xbf"

    readme = (package_dir / "README_ПЕРЕД_ИСПОЛЬЗОВАНИЕМ.txt").read_text(
        encoding="utf-8-sig"
    )
    assert "MDRK BUILDER 1.0.0 INTERNAL" in readme
    assert "как есть" in readme
    assert "issues.txt" in readme


def test_rejects_mismatched_versions(tmp_path: Path) -> None:
    _write_project_version(tmp_path, project="1.0.0", package="0.1.10")
    source_exe = tmp_path / "MDRK_Builder.exe"
    source_exe.write_bytes(b"minimal-test-executable")

    with pytest.raises(RuntimeError, match="Version mismatch"):
        package_internal_release.build_internal_package(
            project_root=tmp_path,
            executable=source_exe,
            dist_dir=tmp_path / "dist",
            replace=False,
        )
