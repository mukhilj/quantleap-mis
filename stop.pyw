"""Stop the hidden dashboard server (whatever process is listening on port 8501)."""
import ctypes
import re
import subprocess
from pathlib import Path

PORT = 8501
PID_FILE = Path(__file__).resolve().parent / "logs" / "server.pid"
NO_WINDOW = 0x08000000


def listening_pids() -> set[str]:
    # Parse `netstat -ano` for processes LISTENING on the dashboard port
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return {m.group(1) for m in re.finditer(rf":{PORT}\s+\S+\s+LISTENING\s+(\d+)", out)}


def main() -> None:
    pids = listening_pids()
    if PID_FILE.exists():
        pids.add(PID_FILE.read_text().strip())
    killed = False
    for pid in filter(None, pids):
        r = subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        killed |= r.returncode == 0
    PID_FILE.unlink(missing_ok=True)
    msg = "Dashboard stopped." if killed else "Dashboard was not running."
    ctypes.windll.user32.MessageBoxW(None, msg, "QuantLeap MIS", 0x40)


if __name__ == "__main__":
    main()
