import uvicorn
uvicorn.run("app.dashboard.api:app", host="127.0.0.1", port=8000, reload=True)
