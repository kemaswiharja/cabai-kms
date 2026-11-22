"""Local dev server: API + static pages on one port (mirrors Vercel routing)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.staticfiles import StaticFiles
from api.index import app
app.mount("/", StaticFiles(directory=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), html=True))
