import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATUS_FILE = Path(os.environ.get("STATUS_FILE", "status.json"))
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
