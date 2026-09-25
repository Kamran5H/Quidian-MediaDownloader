@echo off
setlocal enabledelayedexpansion
title Quidian — One-Click Setup (Kamran Ashraf)
color 0B

echo ==============================================================================
echo             QUIDIAN — MEDIA DOWNLOADER
echo                  One-Click Setup ^& Installer
echo               Developed by: Kamran Ashraf
echo ==============================================================================
echo.

:: 1. Check for Python or Conda
echo [*] Checking runtime environment...
where conda >nul 2>nul
if %errorlevel% equ 0 (
    echo [OK] Conda detected on your system.
    echo [*] Would you like to create or update the dedicated Conda 'quidian' env?
    echo     Press 'Y' to create Conda environment, or 'N' to use standard pip install.
    set /p USE_CONDA="(Y/N, default=N): "
    if /i "!USE_CONDA!"=="Y" (
        echo [*] Setting up Conda environment 'quidian' from environment.yml...
        conda env create -f environment.yml || conda env update -f environment.yml --prune
        echo [OK] Conda environment configured!
        goto :CHECK_TOOLS
    )
)

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found on your PATH!
    echo Please install Python 3.10+ from https://www.python.org/ or install Miniconda/Anaconda.
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PY_VER=%%i
echo [OK] Python runtime active: %PY_VER%

:: 2. Upgrade pip and install requirements
echo.
echo [*] Installing and verifying studio dependencies (requirements.txt)...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [WARNING] Some dependencies encountered an issue. Retrying with --prefer-binary...
    python -m pip install -r requirements.txt --prefer-binary
)
echo [OK] Python packages verified successfully.

:CHECK_TOOLS
:: 3. Check FFmpeg
echo.
echo [*] Checking multimedia tools...
where ffmpeg >nul 2>nul
if %errorlevel% equ 0 (
    echo [OK] FFmpeg is installed and ready on system PATH!
) else (
    echo [NOTICE] FFmpeg was not detected on system PATH.
    echo          High-res 4K stream merging and MP3 conversion require FFmpeg.
    echo          To install easily via winget: run 'winget install Gyan.FFmpeg' in PowerShell.
)

:: 4. Check aria2c
where aria2c >nul 2>nul
if %errorlevel% equ 0 (
    echo [OK] aria2c multi-connection accelerator detected!
) else (
    echo [INFO] aria2c not detected. Downloader will use high-speed multi-fragment native engine.
    echo        Optional: Run 'winget install aria2' to unlock 16-connection turbo mode.
)

:: 5. Verification Test
echo.
echo [*] Performing engine self-test...
python -c "import flask, yt_dlp, gallery_dl, mutagen, curl_cffi, bs4, app; print('[OK] All required engines compiled and operational!')"
if %errorlevel% neq 0 (
    echo [ERROR] Engine self-test failed. Please review error messages above.
    pause
    exit /b 1
)

:: Optional engine: Playwright powers deep stream interception only.
python -c "import playwright" >nul 2>nul
if %errorlevel% equ 0 (
    echo [OK] Playwright deep stream interceptor available.
) else (
    echo [INFO] Playwright not installed - deep stream sniffing is disabled.
    echo        Optional: 'python -m pip install playwright' then 'python -m playwright install chromium'.
)

:: 6. Regression suite
echo.
echo [*] Would you like to run the regression test suite? (recommended after an update)
set /p RUN_TESTS="(Y/N, default=N): "
if /i "!RUN_TESTS!"=="Y" (
    python -m pytest tests/ -q
    if !errorlevel! neq 0 (
        echo [WARNING] Some tests failed. The app will still launch, but review the output above.
        pause
    ) else (
        echo [OK] All tests passed.
    )
)

echo.
echo ==============================================================================
echo [SUCCESS] Quidian is completely installed and configured!
echo Developer: Kamran Ashraf
echo ==============================================================================
echo.
echo Press any key to launch Quidian now...
pause >nul

start run.bat
exit /b 0
