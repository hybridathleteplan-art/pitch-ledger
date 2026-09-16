"""
Vercel entrypoint. Vercel looks for a FastAPI instance named `app` at
supported entrypoints (api/index.py is one of them) - see
https://vercel.com/docs/frameworks/backend/fastapi

This just imports the real app from backend/app.py so there's one copy of
the actual API logic, used both for local dev (uvicorn app:app) and for
the Vercel deployment.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app import app  # noqa: E402  (import after sys.path edit, intentional)
