"""
Human-in-the-Loop Gate

Decides which agent actions require human approval, then pauses execution
and waits for the operator to approve or deny.

Approval can come from two sources (whichever arrives first):
  1. Local file  — operator runs `python -m hitl.approve TOKEN` in a terminal
  2. Email button — operator taps APPROVE / DENY button in the HTML email
"""

import asyncio
import json
import os
import re
import secrets
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PENDING_DIR = Path(__file__).parent / "pending"

# ---------------------------------------------------------------------------
# Gate definitions
# ---------------------------------------------------------------------------

ALWAYS_GATE: Dict[str, str] = {
    "aws_delete_ec2_instance":   "Permanently terminates an EC2 instance (irreversible)",
    "aws_delete_vpc":            "Destroys VPC and all dependent resources (irreversible)",
    "aws_reboot_ec2_instance":   "Reboots an instance — causes downtime",
    "aws_stop_ec2_instance":     "Stops an instance — causes downtime",
    "aws_apply_ec2_rightsizing": "Executes a live instance resize — causes reboot/downtime",
    "aws_create_nat_gateway":    "Commits to ~$0.045/hr NAT Gateway cost (ongoing)",
}

# SSM command patterns that slip through the policy engine but are still risky.
# Each entry: (regex_pattern, human_readable_risk_description)
_HITL_SSM_PATTERNS = [
    (r"iptables\s+-F",              "flushes all firewall rules"),
    (r"ip6tables\s+-F",             "flushes all IPv6 firewall rules"),
    (r"systemctl\s+disable",        "permanently disables a systemd service"),
    (r"truncate\s+-s\s+0",          "truncates a file to zero bytes"),
    (r">\s*/etc/",                  "overwrites a system config file"),
    (r"rm\s+-[rf]+\s+/etc/",       "deletes a system config file"),
    (r"crontab\s+-r",               "deletes all scheduled cron jobs"),
    (r"pkill\s+-f\s+",              "kills processes by name pattern"),
    (r"kill\s+-9\s+1\b",           "kills the init/systemd process"),
    (r"chmod\s+[0-9]*7[0-9]*\s+/", "makes system paths world-writable"),
    (r"passwd\s+\w+",               "changes a user password"),
    (r"visudo",                     "modifies sudoers configuration"),
    (r"chattr\s+",                  "modifies file immutability attributes"),
    (r"dd\s+of=/dev/",              "writes raw data to a block device"),
]


def _ssm_risk_reason(args: Dict[str, Any]) -> Optional[str]:
    """Return a risk description if any SSM command matches a HITL pattern."""
    commands = args.get("commands", [])
    for command in commands:
        cmd = str(command).strip()
        for pattern, description in _HITL_SSM_PATTERNS:
            if re.search(pattern, cmd, flags=re.IGNORECASE):
                preview = cmd[:100] + ("..." if len(cmd) > 100 else "")
                return f"{description}\n  Command: {preview}"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def should_gate(tool_name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Return (gate_required, risk_reason).

    gate_required=True means the action must pause for human approval before
    the MCP tool is invoked.
    """
    if tool_name in ALWAYS_GATE:
        return True, ALWAYS_GATE[tool_name]

    if tool_name == "aws_resize_ec2_instance" and not args.get("dry_run", False):
        target = args.get("target_instance_type", "new type")
        return True, f"Live resize to {target} — causes instance reboot/downtime"

    if tool_name == "aws_ssm_run_command":
        risk = _ssm_risk_reason(args)
        if risk:
            return True, risk

    return False, ""


async def request_human_approval(
    tool_name: str,
    args: Dict[str, Any],
    goal: str,
    risk_reason: str,
    timeout_seconds: int = 300,
) -> bool:
    """
    Pause the agent and wait for a human to approve or deny an action.

    Listens on two channels simultaneously:
      • Local file  — `python -m hitl.approve TOKEN [--deny]`
      • Email reply — operator replies to the notification email with APPROVE or DENY

    Returns True if approved, False if denied or timed out.
    """
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    token        = _make_token()
    pending_file = PENDING_DIR / f"{token}.json"
    pending_file.write_text(json.dumps({
        "token":          token,
        "tool_name":      tool_name,
        "args":           args,
        "goal":           goal,
        "risk_reason":    risk_reason,
        "requested_at":   datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout_seconds,
    }, indent=2))

    # Send email. mailer returns the token (used by IMAP poller to match replies).
    email_sent = False
    try:
        from .mailer import send_approval_email
        send_approval_email(token, tool_name, args, goal, risk_reason, timeout_seconds)
        email_sent = True
    except Exception as exc:
        print(f"⚠️  HITL: Email failed ({exc}) — approve manually via console below.")

    _print_hitl_banner(token, tool_name, risk_reason, timeout_seconds)

    approved_path = PENDING_DIR / f"{token}.approved"
    denied_path   = PENDING_DIR / f"{token}.denied"
    deadline      = time.monotonic() + timeout_seconds

    # IMAP is checked every 15 s (avoid hammering Gmail); local files every 3 s.
    imap_check_interval = 15
    last_imap_check     = 0.0

    while time.monotonic() < deadline:
        # Fast path — local file written by `hitl.approve` CLI
        if approved_path.exists():
            _cleanup(pending_file, approved_path)
            print(f"✅ HITL: Action APPROVED via terminal (token {token})")
            return True
        if denied_path.exists():
            _cleanup(pending_file, denied_path)
            print(f"🚫 HITL: Action DENIED via terminal (token {token})")
            return False

        # Slower path — email button tap (check IMAP every 15 s)
        if email_sent and (time.monotonic() - last_imap_check) >= imap_check_interval:
            last_imap_check = time.monotonic()
            loop = asyncio.get_event_loop()
            try:
                from .imap_poller import check_for_reply
                reply = await loop.run_in_executor(None, check_for_reply, token)
                if reply is True:
                    _cleanup(pending_file)
                    print(f"✅ HITL: Action APPROVED via email reply (token {token})")
                    return True
                if reply is False:
                    _cleanup(pending_file)
                    print(f"🚫 HITL: Action DENIED via email reply (token {token})")
                    return False
            except Exception as exc:
                print(f"⚠️  HITL: IMAP check failed ({exc})")

        await asyncio.sleep(3)

    _cleanup(pending_file)
    print(f"⏰ HITL: Approval timed out after {timeout_seconds}s — action auto-DENIED (token {token})")
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_token(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _cleanup(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def _print_hitl_banner(
    token: str, tool_name: str, risk_reason: str, timeout_seconds: int
) -> None:
    print(f"\n{'=' * 62}")
    print("🛑  HUMAN APPROVAL REQUIRED — AGENT IS PAUSED")
    print(f"    Tool    : {tool_name}")
    print(f"    Risk    : {risk_reason}")
    print(f"    Token   : {token}")
    print(f"    Email   : tap the APPROVE / DENY button in the email")
    print(f"    Or run  : python -m hitl.approve {token}")
    print(f"    Deny    : python -m hitl.approve {token} --deny")
    print(f"    Timeout : {timeout_seconds}s (auto-denied on expiry)")
    print(f"{'=' * 62}\n")
