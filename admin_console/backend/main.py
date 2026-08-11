import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routers import manuals, voc, execution, settings, accounts, files, ops

app = FastAPI(title="Agent Platform Admin Console")

app.include_router(manuals.router)
app.include_router(voc.router)
app.include_router(execution.router)
app.include_router(settings.router)
app.include_router(accounts.router)
app.include_router(files.router)
app.include_router(ops.router)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "../frontend")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
