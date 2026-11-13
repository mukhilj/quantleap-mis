@echo off
REM Double-click to launch the QuantLeap MIS dashboard in your browser.
cd /d "%~dp0"
echo Starting dashboard... (close this window to stop it)
start "" http://localhost:8501
python -m streamlit run app.py
pause
