"""Convenience launcher: python3 run.py [--port 8080] [--reset]  (works from any directory)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server.main import main  # noqa: E402

if __name__ == "__main__":
    main()
