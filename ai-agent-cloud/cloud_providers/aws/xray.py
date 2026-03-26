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
                trace_summaries.append(
                    {
                        "trace_id": item.get("Id"),
                        "start_time": str(item.get("StartTime")),
                        "duration_seconds": item.get("Duration"),
                        "has_fault": item.get("HasFault", False),
                        "has_error": item.get("HasError", False),
                        "has_throttle": item.get("HasThrottle", False),
                        "is_partial": item.get("IsPartial", False),
                        "service_ids": item.get("ServiceIds", []),
                        "response_time": item.get("ResponseTime"),
                    }
                )

            next_token = response.get("NextToken")
            if not next_token:
                break

        return {
            "region": self.region,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "count": len(trace_summaries),
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
