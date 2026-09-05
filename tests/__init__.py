"""Tests for gemini-web2api.

Bootstraps sys.path with the src/ layout so the suite runs from a plain
checkout without installing the package (python -m unittest discover tests).
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
