@echo off
cd /d C:\dev\SpotifyTranscript
call .venv\Scripts\activate
set PYTHONIOENCODING=utf-8

set LOGFILE=logs\run_daily_%date:~-4,4%%date:~-7,2%%date:~0,2%.log

if not exist logs mkdir logs

echo [%date% %time%] Starting sync... >> %LOGFILE% 2>&1
echo [%date% %time%] Starting sync...
python sync.py >> %LOGFILE% 2>&1
if errorlevel 1 (
    echo [%date% %time%] sync.py failed >> %LOGFILE%
    echo [%date% %time%] sync.py failed
    exit /b %errorlevel%
)

echo [%date% %time%] Starting post-processing... >> %LOGFILE% 2>&1
echo [%date% %time%] Starting post-processing...
python post_process.py >> %LOGFILE% 2>&1
if errorlevel 1 (
    echo [%date% %time%] post_process.py failed >> %LOGFILE%
    echo [%date% %time%] post_process.py failed
    exit /b %errorlevel%
)

echo [%date% %time%] Deploying web variant to Hetzner... >> %LOGFILE% 2>&1
echo [%date% %time%] Deploying web variant to Hetzner...
scp "C:\DEV\Obsidian\Nelson\projects\SpotifyTranscript\Transcriptions\AI Daily Brief - Mind Map (Web).html" hetzner:/home/garminbot/SpotifyTranscript/mindmap.html >> %LOGFILE% 2>&1
if errorlevel 1 (
    echo [%date% %time%] scp deploy failed >> %LOGFILE%
    echo [%date% %time%] scp deploy failed
) else (
    echo [%date% %time%] Deploy OK >> %LOGFILE%
    echo [%date% %time%] Deploy OK
)

echo [%date% %time%] Done. >> %LOGFILE% 2>&1
echo [%date% %time%] Done.
