"""AWS X-Ray Manager for trace summary and detail retrieval."""

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError


class XRayManager:
    """Manages AWS X-Ray queries used by the agent for root-cause analysis."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.xray_client = boto3.client("xray", region_name=region)

    def get_trace_summaries(
        self,
        minutes: int = 15,
        max_results: int = 20,
        filter_expression: Optional[str] = None,
        exclude_loopback_only: bool = False,
    ) -> Dict:
        """Get recent X-Ray trace summaries."""
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=minutes)

        trace_summaries = []
        next_token = None

        while len(trace_summaries) < max_results:
            request = {
                "StartTime": start_time,
                "EndTime": end_time,
                "Sampling": False,
            }
            if filter_expression:
                request["FilterExpression"] = filter_expression
            if next_token:
                request["NextToken"] = next_token

            try:
                response = self.xray_client.get_trace_summaries(**request)
            except ClientError as e:
                raise Exception(f"Failed to get X-Ray trace summaries: {e}")

            batch = response.get("TraceSummaries", [])
            for item in batch:
                if len(trace_summaries) >= max_results:
                    break

                service_ids = item.get("ServiceIds", [])
                service_names = self._extract_service_names(service_ids)
                duration_seconds = float(item.get("Duration") or 0.0)
                response_time = float(item.get("ResponseTime") or 0.0)

                if exclude_loopback_only and service_names and all(
                    self._is_loopback_service_name(name) for name in service_names
                ):
                    continue

                trace_summaries.append(
                    {
                        "trace_id": item.get("Id"),
                        "start_time": self._safe_datetime_to_iso(item.get("StartTime")),
                        "duration_seconds": duration_seconds,
                        "has_fault": item.get("HasFault", False),
                        "has_error": item.get("HasError", False),
                        "has_throttle": item.get("HasThrottle", False),
                        "is_partial": item.get("IsPartial", False),
                        "service_ids": service_ids,
                        "service_names": service_names,
                        "response_time": response_time,
                    }
                )

            next_token = response.get("NextToken")
            if not next_token:
                break

        analysis = self._build_trace_analysis(trace_summaries)

        return {
            "region": self.region,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "count": len(trace_summaries),
            "analysis": analysis,
            "trace_summaries": trace_summaries,
        }

    def batch_get_traces(self, trace_ids: List[str]) -> Dict:
        """Get detailed trace documents for supplied trace IDs."""
        if not trace_ids:
            return {"count": 0, "traces": []}

        try:
            response = self.xray_client.batch_get_traces(TraceIds=trace_ids)
        except ClientError as e:
            raise Exception(f"Failed to get X-Ray trace details: {e}")

        traces_out = []
        for trace_item in response.get("Traces", []):
            segments = []
            for segment_wrapper in trace_item.get("Segments", []):
                document_raw = segment_wrapper.get("Document", "")
                document = self._safe_json_load(document_raw)
                segments.append(
                    {
                        "id": segment_wrapper.get("Id"),
                        "document": document,
                    }
                )

            traces_out.append(
                {
                    "id": trace_item.get("Id"),
                    "duration": trace_item.get("Duration"),
                    "limit_exceeded": trace_item.get("LimitExceeded", False),
                    "segment_count": len(segments),
                    "segments": segments,
                }
            )

        return {
            "count": len(traces_out),
            "unprocessed_trace_ids": response.get("UnprocessedTraceIds", []),
            "traces": traces_out,
        }

    def get_service_graph(self, minutes: int = 15) -> Dict:
        """Get a recent X-Ray service graph."""
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=minutes)

        try:
            response = self.xray_client.get_service_graph(
                StartTime=start_time,
                EndTime=end_time,
            )
        except ClientError as e:
            raise Exception(f"Failed to get X-Ray service graph: {e}")

        return {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "services": response.get("Services", []),
        }

    @staticmethod
    def _safe_json_load(value: str):
        try:
            return json.loads(value)
        except Exception:
            return value

    @staticmethod
    def _safe_datetime_to_iso(value) -> Optional[str]:
        if isinstance(value, datetime):
            return value.isoformat()
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _extract_service_names(service_ids: List[Dict]) -> List[str]:
        names = set()
        for service in service_ids:
            name = service.get("Name")
            if name:
                names.add(str(name))

            for nested_name in service.get("Names", []):
                if nested_name:
                    names.add(str(nested_name))

        return sorted(names)

    @staticmethod
    def _is_loopback_service_name(service_name: str) -> bool:
        lowered = service_name.lower()
        return lowered in {"127.0.0.1", "localhost", "::1"}

    def _build_trace_analysis(self, traces: List[Dict]) -> Dict:
        if not traces:
            return {
                "fault_count": 0,
                "error_count": 0,
                "throttle_count": 0,
                "zero_duration_count": 0,
                "non_zero_duration_count": 0,
                "top_service_names": [],
                "warnings": ["No traces found in requested time window."],
            }

        fault_count = sum(1 for t in traces if t.get("has_fault"))
        error_count = sum(1 for t in traces if t.get("has_error"))
        throttle_count = sum(1 for t in traces if t.get("has_throttle"))
        zero_duration_count = sum(1 for t in traces if float(t.get("duration_seconds") or 0.0) <= 0.0)
        non_zero_duration_count = len(traces) - zero_duration_count

        service_frequency = {}
        loopback_only_count = 0

        for trace in traces:
            service_names = trace.get("service_names") or []
            if service_names and all(self._is_loopback_service_name(name) for name in service_names):
                loopback_only_count += 1
            for name in service_names:
                service_frequency[name] = service_frequency.get(name, 0) + 1

        top_service_names = [
            {"name": name, "trace_count": count}
            for name, count in sorted(service_frequency.items(), key=lambda item: item[1], reverse=True)[:10]
        ]

        warnings = []
        if loopback_only_count == len(traces):
            warnings.append(
                "All traces are loopback-only (127.0.0.1/localhost). Add a service-based filter such as service(\"real-api\") to get workload-specific traces."
            )
        if zero_duration_count == len(traces):
            warnings.append(
                "All traces have zero duration in summaries; use trace details to inspect segments and verify end-to-end instrumentation."
            )

        return {
            "fault_count": fault_count,
            "error_count": error_count,
            "throttle_count": throttle_count,
            "zero_duration_count": zero_duration_count,
            "non_zero_duration_count": non_zero_duration_count,
            "loopback_only_count": loopback_only_count,
            "top_service_names": top_service_names,
            "warnings": warnings,
        }
