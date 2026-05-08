"""
HITL Approval CLI

Approve or deny a pending agent action.

Usage:
    python -m hitl.approve TOKEN          # approve
    python -m hitl.approve TOKEN --deny   # deny
"""

import json
import sys
from pathlib import Path

PENDING_DIR = Path(__file__).parent / "pending"


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: python -m hitl.approve TOKEN [--deny]")
        sys.exit(0)

    token = args[0].strip().upper()
    deny  = "--deny" in args

    pending_file = PENDING_DIR / f"{token}.json"
    if not pending_file.exists():
        print(f"❌  No pending action found for token '{token}'")
        print(f"    (pending directory: {PENDING_DIR})")
        sys.exit(1)

    try:
        payload = json.loads(pending_file.read_text())
    except Exception as exc:
        print(f"❌  Could not read pending file: {exc}")
        sys.exit(1)

    verdict      = "denied" if deny else "approved"
    verdict_file = PENDING_DIR / f"{token}.{verdict}"
    verdict_file.touch()

    tool   = payload.get("tool_name", "unknown")
    label  = "DENIED 🚫" if deny else "APPROVED ✅"
    print(f"{label}  {tool}  (token: {token})")


if __name__ == "__main__":
    main()
