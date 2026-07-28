import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATUS_FILE = Path(os.environ.get("STATUS_FILE", "status.json"))
LOG_DIRECTORY = Path(os.environ.get("LOG_DIRECTORY", "logs"))
TRADING_TIMEZONE = os.environ.get("TRADING_TIMEZONE", "America/New_York")
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI()


@app.get("/api/status")
def status() -> JSONResponse:
    try:
        payload = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {"updated_at": None, "message": "Waiting for the bot to write its first status update."}
    except (json.JSONDecodeError, OSError):
        payload = {"updated_at": None, "message": "Status file is mid-write, try again shortly."}
    return JSONResponse(payload)


@app.get("/api/logs")
def logs(lines: int = 200) -> JSONResponse:
    lines = max(1, min(lines, 2000))
    today = datetime.now(ZoneInfo(TRADING_TIMEZONE)).date()
    path = LOG_DIRECTORY / f"{today:%Y}" / f"{today:%m}" / f"{today:%Y-%m-%d}.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return JSONResponse({"lines": [], "path": str(path)})
    tail = text.splitlines()[-lines:]
    return JSONResponse({"lines": tail, "path": str(path)})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
