"""
HITL Email Sender

Sends an HTML email with APPROVE / DENY buttons to the cloud engineer.
Tapping a button opens the email app with a pre-filled reply — one tap to Send.
The IMAP poller then matches the reply by subject token.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict


def _args_rows(tool_name: str, args: Dict[str, Any]) -> str:
    """Build HTML table rows for the most relevant tool arguments."""
    priority = [
        "instance_id", "instance_name", "name",
        "vpc_id", "vpc_name",
        "target_instance_type",
        "commands",
    ]
    rows = []
    for key in priority:
        if key not in args:
            continue
        val = args[key]
        if isinstance(val, list):
            items = [str(v) for v in val[:4]]
            if len(val) > 4:
                items.append(f"… ({len(val)} total)")
            val = "<br>".join(items)
        else:
            val = str(val)
        rows.append(
            f'<tr><td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap">'
            f'<strong>{key}</strong></td>'
            f'<td style="padding:4px 0;font-family:monospace">{val}</td></tr>'
        )

    if not rows:
        for k, v in list(args.items())[:3]:
            rows.append(
                f'<tr><td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap">'
                f'<strong>{k}</strong></td>'
                f'<td style="padding:4px 0;font-family:monospace">{v}</td></tr>'
            )

    return "\n".join(rows)


def send_approval_email(
    token: str,
    tool_name: str,
    args: Dict[str, Any],
    goal: str,
    risk_reason: str,
    timeout_seconds: int,
) -> str:
    """
    Send an HTML approval-request email with clickable APPROVE / DENY buttons.

    Tapping a button opens the engineer's mail app with a pre-filled reply.
    The IMAP poller identifies the reply by searching for the subject pattern
    'HITL-APPROVE-{token}' or 'HITL-DENY-{token}'.

    Returns the token so gate.py can pass it to the IMAP poller.

    Required env vars:
        HITL_SMTP_HOST      — SMTP server (default: smtp.gmail.com)
        HITL_SMTP_PORT      — SMTP port   (default: 587)
        HITL_SMTP_USER      — login username / sender address
        HITL_SMTP_PASSWORD  — login password (Gmail: App Password)
        HITL_EMAIL_TO       — recipient address (the cloud engineer)
    """
    smtp_host = os.getenv("HITL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("HITL_SMTP_PORT", "587"))
    smtp_user = os.getenv("HITL_SMTP_USER", "")
    smtp_pass = os.getenv("HITL_SMTP_PASSWORD", "")
    from_addr = os.getenv("HITL_EMAIL_FROM", smtp_user)
    to_addr   = os.getenv("HITL_EMAIL_TO", "")

    if not to_addr:
        raise ValueError("HITL_EMAIL_TO is not set")
    if not smtp_user or not smtp_pass:
        raise ValueError("HITL_SMTP_USER or HITL_SMTP_PASSWORD is not set")

    action_label = tool_name.replace("aws_", "").replace("_", " ").upper()
    timeout_min  = timeout_seconds // 60
    args_rows    = _args_rows(tool_name, args)

    # mailto: subjects — the IMAP poller searches for exactly these patterns.
    approve_subject = f"HITL-APPROVE-{token}"
    deny_subject    = f"HITL-DENY-{token}"

    # Buttons point to the SENDER address so the reply lands in the agent's inbox.
    approve_href = f"mailto:{smtp_user}?subject={approve_subject}&body=APPROVE"
    deny_href    = f"mailto:{smtp_user}?subject={deny_subject}&body=DENY"

    subject = f"[HITL] {action_label} — token {token}"

    html = f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:30px 10px">
<table width="580" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:8px;overflow:hidden;
              box-shadow:0 2px 8px rgba(0,0,0,.12)">

  <!-- Header -->
  <tr>
    <td style="background:#c0392b;padding:24px 32px">
      <span style="font-size:22px;font-weight:bold;color:#fff">
        ⚠️&nbsp; Agent Approval Required
      </span>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:28px 32px">

      <table cellpadding="0" cellspacing="0" width="100%"
             style="border-left:4px solid #c0392b;padding-left:16px;
                    margin-bottom:24px">
        <tr>
          <td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap">
            <strong>Action</strong>
          </td>
          <td style="padding:4px 0;font-family:monospace">{tool_name}</td>
        </tr>
        <tr>
          <td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap">
            <strong>Risk</strong>
          </td>
          <td style="padding:4px 0;color:#c0392b">{risk_reason}</td>
        </tr>
        <tr>
          <td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap">
            <strong>Goal</strong>
          </td>
          <td style="padding:4px 0">{goal}</td>
        </tr>
      </table>

      <p style="margin:0 0 8px;font-weight:bold;color:#333">Arguments</p>
      <table cellpadding="0" cellspacing="0"
             style="background:#f8f8f8;padding:12px 16px;border-radius:6px;
                    width:100%;margin-bottom:32px">
        {args_rows}
      </table>

      <!-- Buttons -->
      <table cellpadding="0" cellspacing="0" width="100%">
        <tr>
          <td align="center" style="padding-bottom:12px">
            <a href="{approve_href}"
               style="display:inline-block;background:#27ae60;color:#fff;
                      font-size:17px;font-weight:bold;padding:16px 48px;
                      border-radius:6px;text-decoration:none;
                      letter-spacing:.5px">
              ✅&nbsp; APPROVE
            </a>
          </td>
        </tr>
        <tr>
          <td align="center">
            <a href="{deny_href}"
               style="display:inline-block;background:#e74c3c;color:#fff;
                      font-size:17px;font-weight:bold;padding:16px 48px;
                      border-radius:6px;text-decoration:none;
                      letter-spacing:.5px">
              ❌&nbsp; DENY
            </a>
          </td>
        </tr>
      </table>

    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f0f0f0;padding:16px 32px;
               font-size:12px;color:#888;text-align:center">
      Expires in {timeout_min} min &nbsp;·&nbsp; No response = auto-denied
      &nbsp;·&nbsp; Token: <code>{token}</code>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>
"""

    # Plain-text fallback for email clients that block HTML.
    plain = (
        f"Agent action requires approval.\n\n"
        f"Action  : {tool_name}\n"
        f"Risk    : {risk_reason}\n"
        f"Goal    : {goal}\n\n"
        f"APPROVE — send an email to {smtp_user} with subject: {approve_subject}\n"
        f"DENY    — send an email to {smtp_user} with subject: {deny_subject}\n\n"
        f"Or at terminal: python -m hitl.approve {token}\n\n"
        f"Expires in {timeout_min} min. Token: {token}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(from_addr, [to_addr], msg.as_string())

    print(f"📧 HITL: Approval email sent to {to_addr} (token {token})")
    return token
