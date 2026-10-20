@echo off
rem TeloCut (動画編集ソフト) を EXE にビルドして zip にまとめる
rem   build_telocut.bat            3 種類すべて（自分用・無料版・有料版）
rem   build_telocut.bat personal   自分用（キーなしで Studio。配らない）
rem   build_telocut.bat free       無料版（配布用）
rem   build_telocut.bat paid       有料版（購入者用。キーで Pro / Studio）
cd /d %~dp0..
for /f %%v in ('python -c "from editor.version import VERSION; print(VERSION)"') do set VER=%%v
set KINDS=%*
if "%KINDS%"=="" set KINDS=personal free paid
for %%k in (%KINDS%) do (
  call :build %%k || goto :err
)
echo 完了 v%VER%: dist_telocut\^<種類^>\TeloCut\ と dist_telocut\TeloCut_v%VER%_^<種類^>.zip
exit /b 0

:build
echo ==== %1 ====
set TELOCUT_BUILD_KIND=%1
python -m PyInstaller telocut_win.spec --noconfirm --distpath dist_telocut\%1 --workpath build\telocut_work || exit /b 1
dist_telocut\%1\TeloCut\TeloCut.exe --selftest dist_telocut\%1\selftest.txt
type dist_telocut\%1\selftest.txt
findstr /c:"RESULT PASS" dist_telocut\%1\selftest.txt >nul || exit /b 1
copy /Y server\install_server_windows.ps1 dist_telocut\%1\TeloCut\ >nul
copy /Y server\install_render_node.ps1 dist_telocut\%1\TeloCut\ >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist_telocut\%1\TeloCut -DestinationPath dist_telocut\TeloCut_v%VER%_%1.zip"
exit /b 0

:err
echo ビルドに失敗しました
exit /b 1
