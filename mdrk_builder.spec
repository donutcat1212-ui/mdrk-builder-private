from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH)
template_path = (
    project_root
    / "src"
    / "mdrk_builder"
    / "resources"
    / "canonical_mdrk_template.docx"
)
if not template_path.is_file():
    raise FileNotFoundError(f"Canonical MDRK template is missing: {template_path}")
package_data = [(str(template_path), "mdrk_builder/resources")]
hidden_imports = [
    "pythoncom",
    "pywintypes",
    "win32timezone",
    *collect_submodules("win32com"),
]

analysis = Analysis(
    [str(project_root / "src" / "mdrk_builder" / "ui" / "app.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=package_data,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="MDRK_Builder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
