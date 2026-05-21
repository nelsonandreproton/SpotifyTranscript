@echo off
cd /d C:\dev\SpotifyTranscript
call .venv\Scripts\activate

echo [%date% %time%] Starting sync...
python sync.py
if errorlevel 1 (
    echo [%date% %time%] sync.py failed with error %errorlevel%
    exit /b %errorlevel%
)

echo [%date% %time%] Starting post-processing...
python post_process.py
if errorlevel 1 (
    echo [%date% %time%] post_process.py failed with error %errorlevel%
    exit /b %errorlevel%
)

echo [%date% %time%] Done.
