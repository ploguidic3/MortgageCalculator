@echo off
REM ===================================================================
REM  Dota 2 Coaching Watcher launcher (Windows)
REM
REM  Double-click this file to start watching for new matches, OR wire
REM  it into Steam so it launches automatically with Dota 2 (see README,
REM  "Launch automatically with Dota 2").
REM
REM  %~dp0 is the folder this .bat lives in, so it works no matter where
REM  you put the project — as long as coach.py and .env sit next to it.
REM ===================================================================
cd /d "%~dp0"
echo Starting Dota 2 coaching watcher...  (close this window to stop)
python coach.py watch
REM Keep the window open if python exits with an error so you can read it.
pause
