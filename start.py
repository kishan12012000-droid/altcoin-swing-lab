import os
import sys

port = os.environ.get("PORT", "10000")

command = [
    sys.executable,
    "-m",
    "gunicorn",
    "--bind", f"0.0.0.0:{port}",
    "--workers", "1",
    "--threads", "4",
    "--timeout", "25",
    "--graceful-timeout", "10",
    "--access-logfile", "-",
    "--error-logfile", "-",
    "app:app"
]

os.execv(sys.executable, command)