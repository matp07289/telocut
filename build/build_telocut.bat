@echo off
rem TeloCut (動画編集ソフト) を EXE にビルドして zip にまとめる
cd /d %~dp0..
for /f %%v in ('python -c "from editor.version import VERSION; print(VERSION)"') do set VER=%%v
python -m PyInstaller telocut_win.spec --noconfirm --distpath dist_telocut --workpath build\telocut_work || goto :err
dist_telocut\TeloCut\TeloCut.exe --selftest dist_telocut\selftest.txt
type dist_telocut\selftest.txt
copy /Y server\install_server_windows.ps1 dist_telocut\TeloCut\ >nul
copy /Y server\install_render_node.ps1 dist_telocut\TeloCut\ >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist_telocut\TeloCut -DestinationPath dist_telocut\TeloCut_v%VER%.zip"
echo 完了 v%VER%: dist_telocut\TeloCut\ （TeloCut.exe / DavinTelop.exe / TeloCutCLI.exe）
exit /b 0
:err
echo ビルドに失敗しました
exit /b 1
