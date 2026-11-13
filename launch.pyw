"""Double-click launcher: start the dashboard server hidden (if not running) and open it in an app window.

Run with pythonw.exe (no console). Use "Stop QuantLeap MIS" to shut the server down.
"""
import ctypes
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8501
URL = f"http://localhost:{PORT}"
LOG_DIR = ROOT / "logs"
PID_FILE = LOG_DIR / "server.pid"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def port_open() -> bool:
    # True when something is already serving the dashboard port
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def message(text: str, title: str = "QuantLeap MIS") -> None:
    # Native Windows message box (no console available under pythonw)
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def python_exe() -> str:
    # Prefer pythonw.exe next to the running interpreter so the server has no window
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else sys.executable)


def start_server() -> None:
    # Launch Streamlit detached, logging to logs/streamlit.log
    LOG_DIR.mkdir(exist_ok=True)
    log = open(LOG_DIR / "streamlit.log", "w", encoding="utf-8")
    p = subprocess.Popen(
        [python_exe(), "-m", "streamlit", "run", str(ROOT / "app.py"),
         "--server.headless", "true", "--server.port", str(PORT)],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    PID_FILE.write_text(str(p.pid))


def open_window() -> None:
    # App-style window (no tabs/address bar) via Edge or Chrome; fall back to default browser
    candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            subprocess.Popen([exe, f"--app={URL}"])
            return
    webbrowser.open(URL)


def main() -> None:
    if not port_open():
        start_server()
        # Wait up to 60 s for the server to come up
        for _ in range(120):
            if port_open():
                break
            time.sleep(0.5)
        else:
            message(f"Dashboard did not start within 60 s.\nSee {LOG_DIR / 'streamlit.log'}")
            return
    open_window()


if __name__ == "__main__":
    main()
