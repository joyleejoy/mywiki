@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 필요한 것들을 확인합니다...
python -m pip install -r requirements.txt pyinstaller
echo.
echo 위키.exe 를 만듭니다...
python -m PyInstaller --onefile --noconsole --name wiki ^
  --hidden-import pygments.formatters.html --hidden-import pystray._win32 ^
  --noconfirm wiki.py
copy /y dist\wiki.exe "위키.exe"
echo.
echo 완료: %~dp0위키.exe
pause
