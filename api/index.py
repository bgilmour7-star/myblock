"""Vercel Function entry point.

Vercel's Python runtime requires function source files to live under api/, so this
just re-exports the real Flask app from ../app.py (imported by module path, not by
file location, so app.py's own template/static folders still resolve correctly).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
