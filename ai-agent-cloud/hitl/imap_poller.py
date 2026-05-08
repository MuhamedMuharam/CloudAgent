"""
HITL IMAP Reply Poller

Checks the Gmail inbox for an email whose subject contains
'HITL-APPROVE-{token}' or 'HITL-DENY-{token}'.

This is sent automatically when the engineer taps the APPROVE / DENY button
in the HTML email — the mailto: link opens their mail app with the subject
pre-filled, they just tap Send.
"""

import imaplib
import os
from typing import Optional


def check_for_reply(token: str) -> Optional[bool]:
    """
    Search the IMAP inbox for an approval or denial email for this token.

    Returns:
        True   — APPROVE email found
        False  — DENY email found
        None   — nothing yet (or IMAP unavailable)
    """
    host     = os.getenv("HITL_IMAP_HOST", "imap.gmail.com")
    port     = int(os.getenv("HITL_IMAP_PORT", "993"))
    user     = os.getenv("HITL_SMTP_USER", "")
    password = os.getenv("HITL_SMTP_PASSWORD", "")

    if not user or not password:
        return None

    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(user, password)
            imap.select("INBOX")

            approve_subject = f"HITL-APPROVE-{token}"
            deny_subject    = f"HITL-DENY-{token}"

            # Check for approval
            _, data = imap.search(None, f'SUBJECT "{approve_subject}"')
            if data and data[0]:
                for num in data[0].split():
                    imap.store(num, "+FLAGS", "\\Seen")
                return True

            # Check for denial
            _, data = imap.search(None, f'SUBJECT "{deny_subject}"')
            if data and data[0]:
                for num in data[0].split():
                    imap.store(num, "+FLAGS", "\\Seen")
                return False

    except Exception:
        # IMAP errors are non-fatal — agent falls back to local file polling.
        pass

    return None
