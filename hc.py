#!/usr/bin/env python3
"""Dev shim so `python3 hc.py ...` works without installing. The real entry
point is `hc` (see pyproject.toml -> hconv.cli:main)."""
from hconv.cli import main

if __name__ == "__main__":
    main()
