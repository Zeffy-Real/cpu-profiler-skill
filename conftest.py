"""Root-level conftest.py - adds project root to sys.path for src imports."""
import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
