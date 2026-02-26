@echo off
REM =============================================================================
REM DavinTelop Windows ビルドスクリプト
REM 生成物: dist\DavinTelop.exe (onefile)
REM
REM 前提:
REM   pip install pyinstaller pillow
REM =============================================================================
setlocal enabledelayedexpansion

set VERSION=1.0.0
set APP_NAME=DavinTelop

REM プロジェクトルートへ移動 (build\ の親)
cd /d "%~dp0.."

echo =============================================
echo   DavinTelop Windows Build v%VERSION%
echo =============================================

REM ---- [1/3] 依存チェック ----
echo.
echo [1/3] 依存チェック...

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python が見つかりません。Python 3.x をインストールしてください。
    exit /b 1
)
echo   [OK] Python

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller が見つかりません。実行してください: pip install pyinstaller
    exit /b 1
)
echo   [OK] PyInstaller

python -c "import PIL" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Pillow が見つかりません。実行してください: pip install Pillow
    exit /b 1
)
echo   [OK] Pillow

REM ---- [2/3] アイコン生成 ----
echo.
echo [2/3] アイコン生成...
python build\create_icon.py win
if errorlevel 1 (
    echo ERROR: アイコン生成に失敗しました
    exit /b 1
)

REM ---- [3/3] PyInstaller ビルド ----
echo.
echo [3/3] PyInstaller ビルド...
python -m PyInstaller davintelop_win.spec --noconfirm --clean
if errorlevel 1 (
    echo ERROR: PyInstaller ビルドに失敗しました
    exit /b 1
)

set EXE_PATH=dist\%APP_NAME%.exe
if not exist "%EXE_PATH%" (
    echo ERROR: ビルド失敗 -- %EXE_PATH% が見つかりません
    exit /b 1
)

echo.
echo =============================================
echo   ビルド完了!
echo   EXE: %EXE_PATH%
echo =============================================
echo.
echo 動作確認:
echo   %EXE_PATH%

endlocal
