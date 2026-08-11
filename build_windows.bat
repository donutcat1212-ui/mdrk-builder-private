@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo [1/9] Looking for Python 3.12 or 3.11...
set "MDRK_PYTHON="
py -3.12 -c "import struct,sys; raise SystemExit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "MDRK_PYTHON=py -3.12"
if not defined MDRK_PYTHON (
  py -3.11 -c "import struct,sys; raise SystemExit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
  if not errorlevel 1 set "MDRK_PYTHON=py -3.11"
)
if not defined MDRK_PYTHON goto :python_error
%MDRK_PYTHON% -c "import sys; print(sys.version)" || goto :error

if not exist ".venv-win\Scripts\python.exe" (
  echo [2/9] Creating .venv-win...
  %MDRK_PYTHON% -m venv .venv-win || goto :error
) else (
  echo [2/9] Reusing .venv-win...
)

call ".venv-win\Scripts\activate.bat" || goto :error
python -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) and struct.calcsize('P') == 8 else 1)" || goto :venv_error

echo [3/9] Installing project and build dependencies...
python -m pip install --upgrade pip || goto :error
python -m pip install -e ".[dev]" || goto :error

echo [4/9] Compiling and testing...
python -m compileall -q src || goto :error
python -m pytest || goto :error

echo [5/9] Running the fail-closed privacy gate...
python tools\privacy_gate.py || goto :error

echo [6/9] Checking Windows runtime imports...
python -c "import tkinter; import pythoncom; import win32com.client; import mdrk_builder.ui.app" || goto :error

echo [7/9] Building MDRK_Builder.exe...
python -m PyInstaller --noconfirm --clean mdrk_builder.spec || goto :error

echo [8/9] Running packaged UI smoke test...
set "MDRK_BUILDER_SMOKE_REPORT=%CD%\dist\smoke-report.txt"
if exist "%MDRK_BUILDER_SMOKE_REPORT%" del "%MDRK_BUILDER_SMOKE_REPORT%"
"dist\MDRK_Builder.exe" --smoke-test-ui || goto :smoke_error
set "MDRK_BUILDER_SMOKE_REPORT="

echo [9/9] Preparing the internal distribution folder...
python tools\package_internal_release.py --replace || goto :error

echo.
echo SUCCESS: copy the MDRK_Builder_X.Y.Z_Internal folder from %CD%\dist
echo Before pilot use, smoke-test this folder on a clean Windows profile with desktop Word.
exit /b 0

:error
echo.
echo BUILD FAILED with exit code %ERRORLEVEL%.
exit /b 1

:python_error
echo.
echo BUILD FAILED: install 64-bit Python 3.12 or 3.11 from python.org.
exit /b 1

:venv_error
echo.
echo BUILD FAILED: .venv-win has an unsupported Python version or architecture.
echo Remove .venv-win manually, then run this script again.
exit /b 1

:smoke_error
set "MDRK_SMOKE_EXIT=%ERRORLEVEL%"
if exist "%MDRK_BUILDER_SMOKE_REPORT%" type "%MDRK_BUILDER_SMOKE_REPORT%"
echo.
echo PACKAGED UI SMOKE FAILED with exit code %MDRK_SMOKE_EXIT%.
exit /b %MDRK_SMOKE_EXIT%
