@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo [1/7] Looking for Python 3.12 or 3.11...
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
  echo [2/7] Creating .venv-win...
  %MDRK_PYTHON% -m venv .venv-win || goto :error
) else (
  echo [2/7] Reusing .venv-win...
)

call ".venv-win\Scripts\activate.bat" || goto :error
python -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) and struct.calcsize('P') == 8 else 1)" || goto :venv_error

echo [3/7] Installing project and build dependencies...
python -m pip install --upgrade pip || goto :error
python -m pip install -e ".[dev]" || goto :error

echo [4/7] Compiling and testing...
python -m compileall -q src || goto :error
python -m pytest || goto :error

echo [5/7] Checking Windows runtime imports...
python -c "import tkinter; import pythoncom; import win32com.client; import mdrk_builder.ui.app" || goto :error

echo [6/7] Building MDRK_Builder.exe...
python -m PyInstaller --noconfirm --clean mdrk_builder.spec || goto :error

echo [7/7] Running packaged UI smoke test...
"dist\MDRK_Builder.exe" --smoke-test || goto :error

echo.
echo SUCCESS: %CD%\dist\MDRK_Builder.exe
echo Test this EXE with anonymized DOCX, DOC and RTF before clinic use.
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
