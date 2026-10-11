@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo =============================================
echo JapanMarketPulse - 5-axis edition
echo =============================================

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo ERROR: Python was not found. Install Python 3.10 or newer and enable Add Python to PATH.
    pause
    exit /b 1
  )
  set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating an isolated Python environment...
  %PY% -m venv .venv
  if errorlevel 1 goto :error
)

echo [2/3] Installing required libraries...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [3/3] Starting Streamlit...
".venv\Scripts\python.exe" -m streamlit run app.py
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo STARTUP FAILED. Read the error message above.
echo If installation failed, check your internet connection and Python installation.
pause
exit /b 1
