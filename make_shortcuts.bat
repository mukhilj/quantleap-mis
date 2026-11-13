@echo off
REM One-time: create "QuantLeap MIS" (open) and "Stop QuantLeap MIS" shortcuts here and on the Desktop.
cd /d "%~dp0"
for /f "delims=" %%P in ('python -c "import sys,pathlib;print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"') do set PYW=%%P
if not exist "%PYW%" (echo pythonw.exe not found - is Python installed and on PATH? & pause & exit /b 1)
powershell -NoProfile -Command ^
  "$root=(Get-Location).Path; $ws=New-Object -ComObject WScript.Shell; $desk=[Environment]::GetFolderPath('Desktop');" ^
  "foreach($d in @($root,$desk)){ foreach($s in @(@('QuantLeap MIS','launch.pyw'),@('Stop QuantLeap MIS','stop.pyw'))){" ^
  " $l=$ws.CreateShortcut((Join-Path $d ($s[0]+'.lnk'))); $l.TargetPath='%PYW%'; $l.Arguments='\"'+(Join-Path $root $s[1])+'\"';" ^
  " $l.WorkingDirectory=$root; $l.IconLocation=(Join-Path $root 'qlmis.ico')+',0'; $l.Save() } }"
echo Shortcuts created in this folder and on your Desktop.
pause
