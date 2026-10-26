#!/usr/bin/env python3
"""SessionStart hook: inject the matching knowledge-base index."""

import sys

sys.dont_write_bytecode = True
from pathlib import Path


def run() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from qkb_core import main

    return main(["hook-session-start"])


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception as exc:
        print("qoder-knowledge-bridge session hook: {}".format(exc), file=sys.stderr)
        raise SystemExit(0)
