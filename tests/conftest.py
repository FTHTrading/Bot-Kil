"""
tests/conftest.py — Shared pytest configuration for the kalishi-edge test suite.
"""
import sys
from pathlib import Path

# Ensure project root is on sys.path so `engine.*` imports work when pytest
# is invoked from any working directory.
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
