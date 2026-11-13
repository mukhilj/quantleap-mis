@echo off
REM Double-click to import any new QuantLead CSVs from this folder or inbox\.
cd /d "%~dp0"
python import_now.py --preview
echo.
set /p ok=Apply this import? (Y/N): 
if /i "%ok%"=="Y" python import_now.py
pause
