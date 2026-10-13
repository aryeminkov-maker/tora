@echo off
chcp 65001 >nul
REM הרצת ייבוא יוטיוב מהמחשב (אם יוטיוב חוסם את השרתים של GitHub)
REM דרוש: Python, Git, ffmpeg, וקובץ service-account.json בתיקייה הראשית של המאגר (לא מועלה ל-GitHub)
cd /d "%~dp0\.."
git pull --quiet
python -m pip install -q -U yt-dlp firebase-admin
set PAGES_BASE=https://aryeminkov-maker.github.io/tora/
python tools\yt_import.py
pause
