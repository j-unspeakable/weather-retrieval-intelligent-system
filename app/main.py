"""FastAPI application entry point."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routers import health, weather


logging.basicConfig(level=logging.INFO)

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Weather Intelligence", version="0.1.0")
app.state.templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.include_router(health.router)
app.include_router(weather.router)
