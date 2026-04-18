"""
Scheduled Cost Optimization Worker

Runs one autonomous cost-optimization cycle using existing MCP AWS tools.
Designed for systemd timer execution (weekly/biweekly), not an always-on loop.

Modes:
- recommend_only: analyze and log recommendations only
- take_action: analyze, then apply rightsizing for qualifying instances
"""

import ast
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

from agent.mcp_client import MCPClientManager
from agent.state_manager import StateManager


SERVICE_STATE_FILE = Path(__file__).parent / "state" / "cost_optimization_service_state.json"
CLOUDWATCH_PERIOD_CANDIDATES_SECONDS = [60, 300, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]


def _configure_logging() -> logging.Logger:
    log_level_name = os.getenv("COST_OPTIMIZATION_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
    )
    return logging.getLogger("cost_optimization_worker")


def _log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": logging.getLevelName(level),
        "event": event,
    }
    payload.update(fields)
    logger.log(level, json.dumps(payload, ensure_ascii=True))


def _parse_bool_env(var_name: str, default: bool) -> bool:
    raw = os.getenv(var_name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_env(var_name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(var_name)
    if raw is None:
        return max(default, minimum)

    try:
        parsed = int(str(raw).strip())
    except ValueError:
        return max(default, minimum)

    return max(parsed, minimum)


def _parse_float_env(var_name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(var_name)
    if raw is None:
        return max(default, minimum)

    try:
        parsed = float(str(raw).strip())
    except ValueError:
        return max(default, minimum)

    return max(parsed, minimum)


def _parse_csv_env(var_name: str) -> List[str]:
    raw = os.getenv(var_name, "")
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _parse_optional_int_env(var_name: str) -> Optional[int]:
    raw = os.getenv(var_name)
    if raw is None:
        return None

    value = str(raw).strip()
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def _normalize_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()

    recommend_aliases = {"recommend_only", "recommend", "analyze_only", "analysis_only"}
    action_aliases = {"take_action", "action", "execute", "apply"}

    if normalized in recommend_aliases:
        return "recommend_only"
    if normalized in action_aliases:
        return "take_action"

    return "recommend_only"


def _safe_parse_tool_result(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw

    text = str(raw).strip()
    if not text:
        return {"success": False, "error": "empty_tool_result"}

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    return {
        "success": False,
        "error": "unable_to_parse_tool_result",
        "raw": text,
    }


def _without_none_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _read_service_state() -> Dict[str, Any]:
    if not SERVICE_STATE_FILE.exists():
        return {}

    try:
        with open(SERVICE_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
            if isinstance(payload, dict):
                return payload
    except Exception:
        return {}

    return {}


def _write_service_state(payload: Dict[str, Any]) -> None:
    SERVICE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SERVICE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=True)


def _is_due_for_interval(interval_weeks: int) -> Tuple[bool, Optional[str]]:
    if interval_weeks <= 1:
        return True, None

    state = _read_service_state()
    last_success_raw = state.get("last_success_completed_at")
    if not last_success_raw:
        return True, None

    try:
        last_success = datetime.fromisoformat(str(last_success_raw).replace("Z", "+00:00"))
    except Exception:
        return True, None

    now_utc = datetime.now(timezone.utc)
    required_seconds = interval_weeks * 7 * 24 * 60 * 60
    elapsed_seconds = (now_utc - last_success).total_seconds()

    if elapsed_seconds >= required_seconds:
        return True, None

    remaining_seconds = int(required_seconds - elapsed_seconds)
    remaining_days = remaining_seconds // (24 * 60 * 60)

    return False, f"next run due in approximately {remaining_days} day(s)"


def _estimate_datapoint_count(window_minutes: int, period_seconds: int) -> int:
    window_seconds = max(1, int(window_minutes)) * 60
    safe_period_seconds = max(60, int(period_seconds))
    return max(1, (window_seconds + safe_period_seconds - 1) // safe_period_seconds)


def _resolve_analysis_period_seconds(
    analysis_minutes: int,
    requested_period_raw: Optional[str],
    adaptive_base_period_seconds: int,
    max_datapoints_per_metric: int,
) -> Tuple[int, str]:
    requested = str(requested_period_raw or "").strip().lower()
    if requested and requested not in {"auto", "adaptive", "smart"}:
        try:
            explicit_period = int(requested)
            if explicit_period > 0:
                explicit_period = max(60, explicit_period)
                explicit_period = ((explicit_period + 59) // 60) * 60
                return explicit_period, "explicit"
        except ValueError:
            pass

    window_seconds = max(1, analysis_minutes) * 60
    safe_target_datapoints = max(200, max_datapoints_per_metric)
    minimum_period_from_datapoint_budget = (window_seconds + safe_target_datapoints - 1) // safe_target_datapoints

    candidate_floor = max(60, adaptive_base_period_seconds, minimum_period_from_datapoint_budget)
    rounded_candidate_floor = ((candidate_floor + 59) // 60) * 60

    for candidate in CLOUDWATCH_PERIOD_CANDIDATES_SECONDS:
        if candidate >= rounded_candidate_floor:
            return candidate, "adaptive"

    return rounded_candidate_floor, "adaptive"


def _extract_primary_datapoint_count(utilization_analysis: Dict[str, Any]) -> int:
    datapoints = (
        utilization_analysis.get("metrics_summary", {}).get("datapoint_count", {})
        if isinstance(utilization_analysis, dict)
        else {}
    )

    cpu_count = int(datapoints.get("cpu", 0) or 0)
    network_in_count = int(datapoints.get("network_in", 0) or 0)
    network_out_count = int(datapoints.get("network_out", 0) or 0)

    return min(cpu_count, network_in_count, network_out_count)


def _select_resize_candidates(
    instances: List[Dict[str, Any]],
    allowed_instance_ids: Set[str],
    min_monthly_savings: float,
    min_primary_datapoints: int,
    require_downsize_signal: bool,
    require_no_extended_findings: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    selected = []
    skipped = []

    for instance in instances:
        instance_id = str(instance.get("instance_id") or "")
        current_type = str(instance.get("current_instance_type") or "")

        recommendation = instance.get("recommendation", {}) if isinstance(instance, dict) else {}
        utilization = instance.get("utilization_analysis", {}) if isinstance(instance, dict) else {}

        target_type = str(recommendation.get("recommended_instance_type") or "")
        estimated_monthly_savings = float(recommendation.get("estimated_monthly_savings", 0.0) or 0.0)
        utilization_signal = str(utilization.get("recommendation") or "")
        extended_findings = utilization.get("extended_signal_findings", []) if isinstance(utilization, dict) else []
        primary_points = _extract_primary_datapoint_count(utilization)

        skip_reasons = []

        if not instance_id:
            skip_reasons.append("missing_instance_id")
        if allowed_instance_ids and instance_id not in allowed_instance_ids:
            skip_reasons.append("instance_not_in_allowlist")
        if not target_type or target_type == current_type:
            skip_reasons.append("no_better_target")
        if estimated_monthly_savings < min_monthly_savings:
            skip_reasons.append("below_min_monthly_savings")
        if primary_points < min_primary_datapoints:
            skip_reasons.append("insufficient_primary_datapoints")
        if require_downsize_signal and utilization_signal != "downsize":
            skip_reasons.append("no_downsize_signal")
        if require_no_extended_findings and extended_findings:
            skip_reasons.append("extended_signal_risk")

        if skip_reasons:
            skipped.append(
                {
                    "instance_id": instance_id,
                    "current_instance_type": current_type,
                    "target_instance_type": target_type,
                    "estimated_monthly_savings": estimated_monthly_savings,
                    "skip_reasons": skip_reasons,
                }
            )
            continue

        selected.append(
            {
                "instance_id": instance_id,
                "instance_name": instance.get("instance_name"),
                "current_instance_type": current_type,
                "target_instance_type": target_type,
                "estimated_monthly_savings": estimated_monthly_savings,
                "recommendation": recommendation,
                "utilization_analysis": utilization,
            }
        )

    selected.sort(key=lambda item: float(item.get("estimated_monthly_savings", 0.0)), reverse=True)
    return selected, skipped


async def _run_cost_optimization_cycle(config: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
    mcp_client = MCPClientManager()

    aws_env = {
        "AWS_REGION": config["region"],
        "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", ""),
        "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
    }
    if os.getenv("AWS_PROFILE"):
        aws_env["AWS_PROFILE"] = os.getenv("AWS_PROFILE", "")

    aws_server_path = str((Path(__file__).parent / "mcp_servers" / "aws_server.py").resolve())

    try:
        await mcp_client.connect_to_server(
            "aws",
            sys.executable,
            [aws_server_path],
            env=aws_env,
        )
        await mcp_client.discover_capabilities()

        analysis_args = _without_none_fields({
            "minutes": config["analysis_minutes"],
            "period_seconds": config["analysis_period_seconds"],
            "cpu_idle_threshold_percent": config["cpu_idle_threshold_percent"],
            "cpu_hot_threshold_percent": config["cpu_hot_threshold_percent"],
            "network_idle_threshold_bytes_per_period": config["network_idle_threshold_bytes_per_period"],
            "include_memory_disk_signals": config["include_memory_disk_signals"],
            "memory_pressure_threshold_percent": config["memory_pressure_threshold_percent"],
            "disk_pressure_threshold_percent": config["disk_pressure_threshold_percent"],
            "swap_pressure_threshold_percent": config["swap_pressure_threshold_percent"],
            "allowed_families": config["allowed_families"] or None,
            "max_instances": config["max_instances"],
        })

        _log_event(logger, logging.INFO, "fleet_analysis_started", mode=config["mode"], args=analysis_args)

        analysis_raw = await mcp_client.call_tool("aws_analyze_ec2_fleet_cost_optimization", analysis_args)
        analysis = _safe_parse_tool_result(analysis_raw)

        if not analysis.get("success"):
            return {
                "success": False,
                "mode": config["mode"],
                "error": "fleet_analysis_failed",
                "analysis_result": analysis,
            }

        instances = analysis.get("instances", []) if isinstance(analysis, dict) else []
        total_hourly_savings = float(analysis.get("estimated_hourly_savings", 0.0) or 0.0)
        total_monthly_savings = float(analysis.get("estimated_monthly_savings", 0.0) or 0.0)

        selected_candidates, skipped_candidates = _select_resize_candidates(
            instances=instances,
            allowed_instance_ids=config["allowed_instance_ids"],
            min_monthly_savings=config["min_monthly_savings"],
            min_primary_datapoints=config["min_primary_datapoints"],
            require_downsize_signal=config["require_downsize_signal"],
            require_no_extended_findings=config["require_no_extended_findings"],
        )

        cycle_result = {
            "success": True,
            "mode": config["mode"],
            "fleet_analysis": analysis,
            "summary": {
                "instance_count": len(instances),
                "candidate_count": len(selected_candidates),
                "skipped_count": len(skipped_candidates),
                "estimated_hourly_savings": total_hourly_savings,
                "estimated_monthly_savings": total_monthly_savings,
            },
            "analysis_resolution": {
                "window_minutes": config["analysis_minutes"],
                "period_seconds": config["analysis_period_seconds"],
                "period_mode": config["analysis_period_mode"],
                "estimated_primary_datapoints": config["estimated_primary_datapoints"],
                "max_datapoints_per_metric": config["max_datapoints_per_metric"],
            },
            "selected_candidates": selected_candidates,
            "skipped_candidates": skipped_candidates,
            "applied_actions": [],
        }

        if config["mode"] != "take_action":
            _log_event(
                logger,
                logging.INFO,
                "recommendation_cycle_completed",
                candidate_count=len(selected_candidates),
                skipped_count=len(skipped_candidates),
                estimated_monthly_savings=total_monthly_savings,
            )
            return cycle_result

        max_actions = config["max_actions_per_run"]
        action_targets = selected_candidates[:max_actions]

        _log_event(
            logger,
            logging.INFO,
            "take_action_cycle_started",
            requested_candidates=len(selected_candidates),
            max_actions=max_actions,
            executing=len(action_targets),
        )

        for candidate in action_targets:
            instance_id = candidate.get("instance_id")
            apply_args = _without_none_fields({
                "instance_id": instance_id,
                "min_cpu": config["min_cpu"],
                "min_ram_gb": config["min_ram_gb"],
                "allowed_families": config["allowed_families"] or None,
                "create_backup": config["create_backup"],
                "backup_name_prefix": config["backup_name_prefix"],
                "no_reboot_backup": config["no_reboot_backup"],
                "prefer_downsize_when_idle": True,
                "minutes": config["analysis_minutes"],
                "period_seconds": config["analysis_period_seconds"],
                "ensure_service_continuity": config["ensure_service_continuity"],
                "strict_service_continuity": config["strict_service_continuity"],
                "service_recovery_timeout_seconds": config["service_recovery_timeout_seconds"],
            })

            _log_event(
                logger,
                logging.INFO,
                "apply_rightsizing_started",
                instance_id=instance_id,
                target_type=candidate.get("target_instance_type"),
                estimated_monthly_savings=candidate.get("estimated_monthly_savings"),
            )

            apply_raw = await mcp_client.call_tool("aws_apply_ec2_rightsizing", apply_args)
            apply_result = _safe_parse_tool_result(apply_raw)

            action_payload = {
                "instance_id": instance_id,
                "apply_args": apply_args,
                "apply_result": apply_result,
            }
            cycle_result["applied_actions"].append(action_payload)

            _log_event(
                logger,
                logging.INFO if apply_result.get("success") else logging.WARNING,
                "apply_rightsizing_finished",
                instance_id=instance_id,
                success=bool(apply_result.get("success")),
                mode=apply_result.get("mode"),
                estimated_monthly_savings=apply_result.get("estimated_monthly_savings"),
            )

        return cycle_result

    finally:
        await mcp_client.close()


def run_cost_optimization_worker() -> int:
    load_dotenv()
    logger = _configure_logging()
    state_manager = StateManager()

    mode = _normalize_mode(os.getenv("COST_OPTIMIZATION_MODE", "recommend_only"))
    interval_weeks = _parse_int_env("COST_OPTIMIZATION_INTERVAL_WEEKS", 1, minimum=1)
    analysis_minutes = _parse_int_env("COST_OPTIMIZATION_ANALYSIS_MINUTES", 10080, minimum=60)
    requested_analysis_period = os.getenv("COST_OPTIMIZATION_ANALYSIS_PERIOD_SECONDS", "auto")
    adaptive_base_period_seconds = _parse_int_env(
        "COST_OPTIMIZATION_ADAPTIVE_BASE_PERIOD_SECONDS",
        900,
        minimum=60,
    )
    max_datapoints_per_metric = _parse_int_env(
        "COST_OPTIMIZATION_MAX_DATAPOINTS_PER_METRIC",
        1200,
        minimum=200,
    )
    analysis_period_seconds, analysis_period_mode = _resolve_analysis_period_seconds(
        analysis_minutes=analysis_minutes,
        requested_period_raw=requested_analysis_period,
        adaptive_base_period_seconds=adaptive_base_period_seconds,
        max_datapoints_per_metric=max_datapoints_per_metric,
    )
    estimated_primary_datapoints = _estimate_datapoint_count(
        window_minutes=analysis_minutes,
        period_seconds=analysis_period_seconds,
    )

    config = {
        "region": os.getenv("AWS_REGION", "us-east-1"),
        "mode": mode,
        "interval_weeks": interval_weeks,
        "analysis_minutes": analysis_minutes,
        "analysis_period_seconds": analysis_period_seconds,
        "analysis_period_mode": analysis_period_mode,
        "max_datapoints_per_metric": max_datapoints_per_metric,
        "estimated_primary_datapoints": estimated_primary_datapoints,
        "cpu_idle_threshold_percent": _parse_float_env("COST_OPTIMIZATION_CPU_IDLE_THRESHOLD_PERCENT", 15.0, minimum=1.0),
        "cpu_hot_threshold_percent": _parse_float_env("COST_OPTIMIZATION_CPU_HOT_THRESHOLD_PERCENT", 70.0, minimum=1.0),
        "network_idle_threshold_bytes_per_period": _parse_float_env(
            "COST_OPTIMIZATION_NETWORK_IDLE_THRESHOLD_BYTES_PER_PERIOD",
            150000.0,
            minimum=1.0,
        ),
        "include_memory_disk_signals": _parse_bool_env("COST_OPTIMIZATION_INCLUDE_MEMORY_DISK_SIGNALS", True),
        "memory_pressure_threshold_percent": _parse_float_env(
            "COST_OPTIMIZATION_MEMORY_PRESSURE_THRESHOLD_PERCENT",
            75.0,
            minimum=1.0,
        ),
        "disk_pressure_threshold_percent": _parse_float_env(
            "COST_OPTIMIZATION_DISK_PRESSURE_THRESHOLD_PERCENT",
            80.0,
            minimum=1.0,
        ),
        "swap_pressure_threshold_percent": _parse_float_env(
            "COST_OPTIMIZATION_SWAP_PRESSURE_THRESHOLD_PERCENT",
            50.0,
            minimum=1.0,
        ),
        "allowed_families": _parse_csv_env("COST_OPTIMIZATION_ALLOWED_FAMILIES"),
        "allowed_instance_ids": set(_parse_csv_env("COST_OPTIMIZATION_ALLOWED_INSTANCE_IDS")),
        "max_instances": _parse_int_env("COST_OPTIMIZATION_MAX_INSTANCES", 100, minimum=1),
        "min_monthly_savings": _parse_float_env("COST_OPTIMIZATION_MIN_MONTHLY_SAVINGS_USD", 10.0, minimum=0.0),
        "min_primary_datapoints": _parse_int_env("COST_OPTIMIZATION_MIN_PRIMARY_DATAPOINTS", 24, minimum=1),
        "max_actions_per_run": _parse_int_env("COST_OPTIMIZATION_MAX_ACTIONS_PER_RUN", 2, minimum=1),
        "require_downsize_signal": _parse_bool_env("COST_OPTIMIZATION_REQUIRE_DOWNSIZE_SIGNAL", True),
        "require_no_extended_findings": _parse_bool_env("COST_OPTIMIZATION_REQUIRE_NO_EXTENDED_FINDINGS", True),
        "min_cpu": _parse_optional_int_env("COST_OPTIMIZATION_MIN_CPU"),
        "min_ram_gb": _parse_optional_int_env("COST_OPTIMIZATION_MIN_RAM_GB"),
        "create_backup": _parse_bool_env("COST_OPTIMIZATION_CREATE_BACKUP", True),
        "backup_name_prefix": os.getenv("COST_OPTIMIZATION_BACKUP_NAME_PREFIX", "ai-agent-cost-opt"),
        "no_reboot_backup": _parse_bool_env("COST_OPTIMIZATION_NO_REBOOT_BACKUP", True),
        "ensure_service_continuity": _parse_bool_env("COST_OPTIMIZATION_ENSURE_SERVICE_CONTINUITY", True),
        "strict_service_continuity": _parse_bool_env("COST_OPTIMIZATION_STRICT_SERVICE_CONTINUITY", False),
        "service_recovery_timeout_seconds": _parse_int_env(
            "COST_OPTIMIZATION_SERVICE_RECOVERY_TIMEOUT_SECONDS",
            420,
            minimum=60,
        ),
    }

    _log_event(
        logger,
        logging.INFO,
        "cost_optimization_worker_started",
        mode=config["mode"],
        interval_weeks=config["interval_weeks"],
        analysis_minutes=config["analysis_minutes"],
        analysis_period_seconds=config["analysis_period_seconds"],
        analysis_period_mode=config["analysis_period_mode"],
        estimated_primary_datapoints=config["estimated_primary_datapoints"],
        max_instances=config["max_instances"],
        max_actions_per_run=config["max_actions_per_run"],
    )

    is_due, due_reason = _is_due_for_interval(config["interval_weeks"])
    if not is_due:
        summary = {
            "success": True,
            "mode": config["mode"],
            "status": "skipped_interval",
            "reason": due_reason,
        }

        state_manager.log_action(
            action_type="cost_optimization_service_run",
            details=summary,
            success=True,
        )

        _log_event(logger, logging.INFO, "cost_optimization_worker_skipped", reason=due_reason)
        return 0

    started_at = datetime.now(timezone.utc)

    try:
        cycle_result = asyncio.run(_run_cost_optimization_cycle(config=config, logger=logger))

        cycle_result["status"] = "completed" if cycle_result.get("success") else "failed"
        cycle_result["started_at"] = started_at.isoformat()
        cycle_result["completed_at"] = datetime.now(timezone.utc).isoformat()

        state_manager.log_action(
            action_type="cost_optimization_service_run",
            details=cycle_result,
            success=bool(cycle_result.get("success")),
            error=cycle_result.get("error"),
        )

        summary = cycle_result.get("summary", {}) if isinstance(cycle_result, dict) else {}

        if cycle_result.get("success"):
            state_manager.log_cost_recommendation(
                recommendation_type="fleet_rightsizing",
                details={
                    "mode": config["mode"],
                    "instance_count": summary.get("instance_count", 0),
                    "candidate_count": summary.get("candidate_count", 0),
                    "skipped_count": summary.get("skipped_count", 0),
                },
                estimated_hourly_savings_usd=float(summary.get("estimated_hourly_savings", 0.0) or 0.0),
                estimated_monthly_savings_usd=float(summary.get("estimated_monthly_savings", 0.0) or 0.0),
            )

            for action in cycle_result.get("applied_actions", []):
                apply_result = action.get("apply_result", {}) if isinstance(action, dict) else {}
                if not apply_result.get("success"):
                    continue
                if str(apply_result.get("mode")) != "applied":
                    continue

                state_manager.log_cost_action_applied(
                    action_type="ec2_rightsizing",
                    details={
                        "instance_id": action.get("instance_id"),
                        "mode": apply_result.get("mode"),
                        "result": apply_result.get("result"),
                        "selection": apply_result.get("selection"),
                    },
                    estimated_hourly_savings_usd=float(apply_result.get("estimated_hourly_savings", 0.0) or 0.0),
                    estimated_monthly_savings_usd=float(apply_result.get("estimated_monthly_savings", 0.0) or 0.0),
                )

            persisted_state = _read_service_state()
            persisted_state.update(
                {
                    "last_run_started_at": cycle_result.get("started_at"),
                    "last_run_completed_at": cycle_result.get("completed_at"),
                    "last_success_completed_at": cycle_result.get("completed_at"),
                    "last_run_mode": config["mode"],
                    "last_run_status": cycle_result.get("status"),
                    "last_run_summary": summary,
                }
            )
            _write_service_state(persisted_state)

            _log_event(
                logger,
                logging.INFO,
                "cost_optimization_worker_completed",
                mode=config["mode"],
                summary=summary,
                applied_actions=len(cycle_result.get("applied_actions", [])),
            )
            return 0

        _log_event(
            logger,
            logging.ERROR,
            "cost_optimization_worker_failed",
            error=cycle_result.get("error"),
            details=cycle_result,
        )
        return 1

    except Exception as exc:
        error_payload = {
            "success": False,
            "mode": config["mode"],
            "status": "failed",
            "error": str(exc),
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        state_manager.log_action(
            action_type="cost_optimization_service_run",
            details=error_payload,
            success=False,
            error=str(exc),
        )

        _log_event(logger, logging.ERROR, "cost_optimization_worker_exception", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cost_optimization_worker())

