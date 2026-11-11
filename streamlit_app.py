"""VendorIQ on Streamlit — serves the original React frontend.

`streamlit run streamlit_app.py` detects the ASGI ``app`` below and serves it with
Streamlit's built-in server. User routes are matched before Streamlit's own, so:

  /            -> React app (webapp/index.html, built with `npm run build:streamlit`)
  /assets/*    -> React JS/CSS bundles
  /api/*       -> the FastAPI backend (backend/app), unchanged
  /_stcore/*   -> Streamlit health/status endpoints used by Streamlit Community Cloud

The React build uses relative URLs (./assets, ./api), so it also works behind the
path prefix that Streamlit Community Cloud proxies apps through.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import streamlit as st
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
WEBAPP = ROOT / "webapp"
sys.path.insert(0, str(ROOT / "backend"))


def _load_secrets_into_env() -> None:
    """Expose root-level Streamlit secrets (e.g. GEMINI_API_KEY) to the backend settings."""
    for candidate in (ROOT / ".streamlit" / "secrets.toml", Path.home() / ".streamlit" / "secrets.toml"):
        if candidate.is_file():
            with candidate.open("rb") as fh:
                for key, value in tomllib.load(fh).items():
                    if isinstance(value, (str, int, float, bool)):
                        os.environ.setdefault(key.upper(), str(value))
    try:  # wherever else Streamlit (or Streamlit Cloud) keeps secrets
        for key, value in st.secrets.items():
            if isinstance(value, (str, int, float, bool)):
                os.environ.setdefault(key.upper(), str(value))
    except Exception:
        pass


_load_secrets_into_env()

from app.main import app as api_app  # noqa: E402  (import after secrets are in the environment)


# Relative URLs (./assets, ./api) need the page URL to end in "/"; behind a proxy prefix
# such as Streamlit Cloud's, the browser path may lack it, so fix it client-side first.
_SLASH_FIX = "<script>if(!location.pathname.endsWith('/'))location.replace(location.pathname+'/'+location.search+location.hash)</script>"

# The Streamlit Cloud "ready" handshake lives in frontend/index.html (built into webapp/).


async def index(request):
    page = (WEBAPP / "index.html").read_text(encoding="utf-8").replace("<head>", "<head>" + _SLASH_FIX, 1)
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


app = st.App(
    "streamlit_ui.py",
    routes=[
        Route("/", index),
        Mount("/assets", app=StaticFiles(directory=WEBAPP / "assets"), name="assets"),
        # A non-function endpoint is passed the raw ASGI scope, so FastAPI still sees /api/... paths.
        Route("/api/{path:path}", endpoint=api_app),
    ],
)
