import fcntl
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

STATUS_FILE = Path(os.environ.get("STATUS_FILE", "status.json"))
LOG_DIRECTORY = Path(os.environ.get("LOG_DIRECTORY", "logs"))
TRADING_TIMEZONE = os.environ.get("TRADING_TIMEZONE", "America/New_York")
COMMAND_FILE = Path(os.environ.get("COMMAND_FILE", "commands.json"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI()


def enqueue_command(command_type: str, **fields) -> str:
    """Appends an action request for the trader process to pick up and
    execute. This dashboard has no Webull credentials or API access of its
    own - it can only ask the trader to act, never place an order directly.
    Both sides share the same file on the same host volume, so an advisory
    file lock keeps this read-modify-write safe against the trader's own.
    """
    command_id = uuid.uuid4().hex
    command = {
        "id": command_id,
        "type": command_type,
        "requested_at": time.time(),
        **fields,
    }
    COMMAND_FILE.parent.mkdir(parents=True, exist_ok=True)
    COMMAND_FILE.touch(exist_ok=True)
    with open(COMMAND_FILE, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            raw = handle.read().strip()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            commands = data.get("commands", [])
            if not isinstance(commands, list):
                commands = []
            commands.append(command)
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"commands": commands}))
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return command_id


class SellRequest(BaseModel):
    symbol: str
    instrument_type: str = "EQUITY"


class WatchlistRequest(BaseModel):
    symbol: str


@app.post("/api/close-all")
def close_all() -> JSONResponse:
    command_id = enqueue_command("close_all")
    return JSONResponse({"queued": True, "id": command_id})


@app.post("/api/sell")
def sell(request: SellRequest) -> JSONResponse:
    symbol = request.symbol.strip().upper()
    if not symbol:
        return JSONResponse({"queued": False, "error": "symbol is required"}, status_code=400)
    command_id = enqueue_command(
        "sell",
        symbol=symbol,
        instrument_type=request.instrument_type.strip().upper() or "EQUITY",
    )
    return JSONResponse({"queued": True, "id": command_id})


@app.post("/api/watchlist")
def watchlist_add(request: WatchlistRequest) -> JSONResponse:
    symbol = request.symbol.strip().upper()
    if not symbol:
        return JSONResponse({"queued": False, "error": "symbol is required"}, status_code=400)
    command_id = enqueue_command("watchlist_add", symbol=symbol)
    return JSONResponse({"queued": True, "id": command_id})


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
