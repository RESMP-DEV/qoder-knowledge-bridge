#!/usr/bin/env python3
"""PostToolUse hook: log qkb and knowledge-MCP reads for provenance."""

import sys

sys.dont_write_bytecode = True
from pathlib import Path


def run() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from qkb_core import main

    return main(["hook-provenance"])


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception as exc:
        print("qoder-knowledge-bridge provenance hook: {}".format(exc), file=sys.stderr)
        raise SystemExit(0)
