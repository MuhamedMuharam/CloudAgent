"""
Observability Helper Agent
==========================

Runs a scoped helper agent for logs/metrics/traces analysis so the
main controller receives compact findings instead of large raw payloads.
"""

import json
import os
from typing import Any, Dict, List, Set

from openai import OpenAI

from .mcp_client import MCPClientManager


OBSERVABILITY_HELPER_MODEL_DEFAULT = "gpt-4o-mini"

# Keep this list read-only/diagnostic focused.
OBSERVABILITY_ALLOWED_TOOLS: Set[str] = {
    "aws_collect_ec2_health_snapshot",
    "aws_ssm_collect_host_diagnostics",
    "aws_list_ec2_instances",
    "aws_get_ec2_instance_status",
    "aws_get_ec2_instance_ssm_status",
    "aws_get_ec2_metrics",
    "aws_list_log_groups",
    "aws_list_log_streams",
    "aws_get_log_events",
    "aws_filter_logs",
    "aws_get_xray_trace_summaries",
    "aws_get_xray_trace_details",
    "aws_get_xray_service_graph",
}


def _select_observability_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter OpenAI-formatted tools to the helper's diagnostic toolset."""
    selected: List[Dict[str, Any]] = []
    for tool in tools:
        function_def = tool.get("function", {})
        name = function_def.get("name", "")
        if name in OBSERVABILITY_ALLOWED_TOOLS:
            selected.append(tool)
    return selected


def _truncate_payload(payload: str, max_chars: int = 12000) -> str:
    """Bound tool result size inside helper context to avoid runaway token growth."""
    if len(payload) <= max_chars:
        return payload

    trimmed = payload[:max_chars]
    removed = len(payload) - max_chars
    return f"{trimmed}\n\n[truncated {removed} chars to keep helper context bounded]"


def _normalize_helper_report(raw_content: str) -> Dict[str, Any]:
    """Normalize helper model output into a strict structured JSON report."""
    text = (raw_content or "").strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    parsed: Dict[str, Any]
    try:
        loaded = json.loads(text)
        parsed = loaded if isinstance(loaded, dict) else {}
    except Exception:
        parsed = {}

    if not parsed:
        parsed = {
            "current_state": {},
            "root_cause": "unknown",
            "confidence": 0.0,
            "evidence": [{"source": "helper_output", "detail": text or "No content produced."}],
            "telemetry_summary": {},
            "data_gaps": ["Helper response was not valid JSON."],
        }

    confidence = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    evidence = parsed.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []

    data_gaps = parsed.get("data_gaps", [])
    if not isinstance(data_gaps, list):
        data_gaps = []

    return {
        "current_state": parsed.get("current_state", {}) if isinstance(parsed.get("current_state", {}), dict) else {},
        "root_cause": str(parsed.get("root_cause", "unknown")),
        "confidence": confidence,
        "evidence": evidence,
        "telemetry_summary": parsed.get("telemetry_summary", {}) if isinstance(parsed.get("telemetry_summary", {}), dict) else {},
        "data_gaps": data_gaps,
    }


async def run_observability_helper(
    goal: str,
    client: OpenAI,
    mcp_client: MCPClientManager,
    model: str = None,
    max_iterations: int = 6,
    analysis_request: str = None,
) -> Dict[str, Any]:
    """
    Execute a focused helper-agent pass for observability-heavy goals.

    Returns a compact summary payload that can be injected into the main
    controller context.
    """
    helper_model = model or os.getenv("OBSERVABILITY_HELPER_MODEL", OBSERVABILITY_HELPER_MODEL_DEFAULT)
    helper_tools = _select_observability_tools(mcp_client.get_tools_for_openai())
    allowed_names = {
        tool.get("function", {}).get("name", "")
        for tool in helper_tools
    }

    if not helper_tools:
        return {
            "success": False,
            "reason": "no_observability_tools_available",
            "summary": "Observability helper skipped: no matching diagnostic tools were discovered.",
            "model": helper_model,
            "iterations": 0,
            "tools_used": [],
        }

    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are the Observability Helper Agent for cloud incidents. "
                "Your job is to run only diagnostic observability tools and produce a compact summary for a controller agent.\n\n"
                "Rules:\n"
                "- Scope MUST follow the controller request. If request is logs-only, do logs-only. If metrics-only, do metrics-only.\n"
                "- Do not call tools outside requested scope unless needed to close a critical data gap.\n"
                "- If scope includes host health/status checks, prefer aws_collect_ec2_health_snapshot first.\n"
                "- For host/system and OS-level checks, use aws_ssm_collect_host_diagnostics when needed.\n"
                "- For disk-pressure scope, collect host diagnostics and extract actionable filesystem evidence: top large files/paths and inode usage.\n"
                "- Include concrete file/path names with sizes in evidence when available; if unavailable, explain why in data_gaps.\n"
                "- Do not call mutating tools.\n"
                "- Do not propose mitigation or remediation actions. Diagnostics only.\n"
                "- If a user goal lacks identifiers, resolve instance IDs via list/status tools first.\n"
                "- For CloudWatch metric queries, choose period_seconds by lookback window to avoid oversampling:\n"
                "  * up to 30 minutes -> 60 seconds\n"
                "  * 31 to 180 minutes -> 300 seconds\n"
                "  * more than 180 minutes -> 900 seconds or more\n"
                "- If request asks for last hour metrics, prefer aws_get_ec2_metrics with period_seconds=300.\n"
                "- Log group routing for incident triage:\n"
                "  * /ai-agent/app -> real-api request flow, input validation, order submission errors\n"
                "  * /ai-agent/worker -> Celery task execution, retries/failures, processing latency\n"
                "  * /ai-agent/otel -> OpenTelemetry collector pipeline/export issues to X-Ray\n"
                "  * /ai-agent/system -> OS/systemd/network/runtime host-level issues\n"
                "  * /ai-agent/agent -> alarm_worker and agent orchestration/decision logs\n"
                "- For root-cause analysis, correlate timestamps across app + worker + otel logs before concluding.\n"
                "- Avoid dumping long raw payloads in the final answer.\n"
                "- Return ONLY valid JSON with this exact shape and no markdown:\n"
                "{\n"
                "  \"current_state\": {\"instance_id\": \"...\", \"instance_name\": \"...\", \"instance_state\": \"...\", \"alarm_state_summary\": \"...\"},\n"
                "  \"root_cause\": \"...\",\n"
                "  \"confidence\": 0.0,\n"
                "  \"evidence\": [{\"source\": \"metrics|logs|traces|alarms\", \"detail\": \"...\"}],\n"
                "  \"telemetry_summary\": {\"metrics\": \"...\", \"logs\": \"...\", \"traces\": \"...\"},\n"
                "  \"data_gaps\": [\"...\"]\n"
                "}"
            ),
        },
        {
            "role": "user",
            "content": (
                "Analyze this observability request and summarize key findings for the controller. "
                "Use diagnostic tools as needed and keep the response concise.\n\n"
                f"Original goal:\n{goal}\n\n"
                f"Controller request:\n{analysis_request or goal}"
            ),
        },
    ]

    tools_used: List[str] = []

    for iteration in range(1, max_iterations + 1):
        response = client.chat.completions.create(
            model=helper_model,
            messages=messages,
            tools=helper_tools,
            tool_choice="auto",
        )

        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for tool_call in msg.tool_calls:
                tool_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments or "{}")

                if tool_name not in allowed_names:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(
                                {
                                    "success": False,
                                    "error": f"Tool '{tool_name}' is not allowed in observability helper.",
                                }
                            ),
                        }
                    )
                    continue

                try:
                    result = await mcp_client.call_tool(tool_name, args)
                    tools_used.append(tool_name)
                    result_text = result if isinstance(result, str) else json.dumps(result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": _truncate_payload(result_text),
                        }
                    )
                except Exception as exc:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"success": False, "error": str(exc)}),
                        }
                    )
            continue

        summary = (msg.content or "").strip()
        report = _normalize_helper_report(summary)

        return {
            "success": True,
            "reason": "completed",
            "summary": json.dumps(report),
            "report": report,
            "model": helper_model,
            "iterations": iteration,
            "tools_used": sorted(set(tools_used)),
        }

    timeout_report = {
        "current_state": {},
        "root_cause": "unknown",
        "confidence": 0.0,
        "evidence": [],
        "telemetry_summary": {},
        "data_gaps": ["Observability helper reached max iterations before producing a final summary."],
    }

    return {
        "success": False,
        "reason": "max_iterations_reached",
        "summary": json.dumps(timeout_report),
        "report": timeout_report,
        "model": helper_model,
        "iterations": max_iterations,
        "tools_used": sorted(set(tools_used)),
    }
