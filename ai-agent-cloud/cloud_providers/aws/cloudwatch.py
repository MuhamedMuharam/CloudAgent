"""
AWS CloudWatch Manager
Handles CloudWatch Metrics, Logs, Alarms, and Dashboards.
"""

import sys
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError


class CloudWatchManager:
    """Manages AWS CloudWatch observability operations."""

    _EC2_METRIC_SPECS = [
        ("CPUUtilization", "Percent", "Average"),
        ("NetworkIn", "Bytes", "Sum"),
        ("NetworkOut", "Bytes", "Sum"),
        ("StatusCheckFailed", "Count", "Maximum"),
        ("StatusCheckFailed_Instance", "Count", "Maximum"),
        ("StatusCheckFailed_System", "Count", "Maximum"),
    ]

    _CW_AGENT_METRIC_SPECS = [
        ("disk_used_percent", "Percent", "Average"),
        ("disk_inodes_free", None, "Average"),
        ("diskio_io_time", "Percent", "Average"),
        ("mem_used_percent", "Percent", "Average"),
        ("swap_used_percent", "Percent", "Average"),
    ]

    def __init__(self, region: str = "us-east-1"):
        """
        Initialize CloudWatch Manager.

        Args:
            region: AWS region (default: us-east-1)
        """
        self.region = region
        self.cloudwatch_client = boto3.client("cloudwatch", region_name=region)
        self.logs_client = boto3.client("logs", region_name=region)
        self.sqs_client = boto3.client("sqs", region_name=region)

    def get_ec2_metrics(
        self,
        instance_id: str,
        minutes: int = 15,
        period_seconds: int = 60,
        namespace: str = "AWS/EC2",
        include_agent_metrics: bool = True,
        agent_namespace: str = "CWAgent",
    ) -> Dict:
        """
        Get key EC2 CloudWatch metrics for an instance.

        Args:
            instance_id: EC2 instance ID
            minutes: Time window to query in minutes
            period_seconds: Metrics period in seconds
            namespace: Primary CloudWatch namespace (default: AWS/EC2)
            include_agent_metrics: Include CloudWatch Agent metrics when True
            agent_namespace: CloudWatch Agent namespace (default: CWAgent)

        Returns:
            Dictionary with metric datapoints
        """
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=minutes)

        try:
            primary_metrics = self._fetch_metric_series(
                namespace=namespace,
                instance_id=instance_id,
                start_time=start_time,
                end_time=end_time,
                period_seconds=period_seconds,
                metric_specs=self._EC2_METRIC_SPECS,
            )

            namespaced_metrics = {namespace: primary_metrics}

            agent_metrics = {}
            if include_agent_metrics:
                agent_metrics = self._fetch_metric_series(
                    namespace=agent_namespace,
                    instance_id=instance_id,
                    start_time=start_time,
                    end_time=end_time,
                    period_seconds=period_seconds,
                    metric_specs=self._CW_AGENT_METRIC_SPECS,
                    use_unit_filter=False,
                    allow_dimension_fallback=True,
                )
                namespaced_metrics[agent_namespace] = agent_metrics

            primary_summary = self._summarize_metric_availability(primary_metrics)
            agent_summary = self._summarize_metric_availability(agent_metrics) if include_agent_metrics else None

            warnings = []
            if include_agent_metrics and agent_summary and agent_summary["metrics_with_data"] == 0:
                warnings.append(
                    "No datapoints found for CloudWatch Agent metrics in the selected window. "
                    "This usually means telemetry is unavailable (agent not running, namespace/dimension mismatch, or delayed publishing), "
                    "not necessarily that disk IO usage is zero."
                )

            return {
                "instance_id": instance_id,
                "namespace": namespace,
                "agent_namespace": agent_namespace if include_agent_metrics else None,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "period_seconds": period_seconds,
                "metrics": primary_metrics,
                "agent_metrics": agent_metrics,
                "namespaced_metrics": namespaced_metrics,
                "availability_summary": {
                    "primary": primary_summary,
                    "agent": agent_summary,
                },
                "interpretation_guardrails": [
                    "Treat empty datapoints as missing telemetry unless corroborated by other evidence.",
                    "Do not conclude low/no activity solely from empty datapoints.",
                ],
                "warnings": warnings,
            }

        except ClientError as e:
            error_msg = f"Failed to fetch EC2 metrics: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def poll_alarm_notifications(
        self,
        queue_url: str,
        max_messages: int = 5,
        wait_time_seconds: int = 5,
        visibility_timeout: int = 60,
        delete_on_read: bool = False,
    ) -> Dict:
        """
        Poll alarm notifications delivered from SNS to SQS.

        Args:
            queue_url: SQS queue URL subscribed to SNS alarm topic
            max_messages: Number of messages to retrieve (1-10)
            wait_time_seconds: Long-poll wait duration
            visibility_timeout: Hide received messages for this duration
            delete_on_read: Delete messages after successful parsing

        Returns:
            Dictionary with normalized alarm notifications
        """
        try:
            response = self.sqs_client.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=max(1, min(max_messages, 10)),
                WaitTimeSeconds=max(0, min(wait_time_seconds, 20)),
                VisibilityTimeout=max(0, visibility_timeout),
                MessageAttributeNames=["All"],
                AttributeNames=["All"],
            )

            messages = response.get("Messages", [])
            notifications = []
            deleted_count = 0

            for message in messages:
                body_raw = message.get("Body", "")
                body = self._safe_json_load(body_raw)

                sns_envelope = body if isinstance(body, dict) and body.get("Type") == "Notification" else {}
                sns_payload_raw = sns_envelope.get("Message", body_raw)
                sns_payload = self._safe_json_load(sns_payload_raw)

                notification = {
                    "sqs_message_id": message.get("MessageId"),
                    "receipt_handle": message.get("ReceiptHandle"),
                    "sns_message_id": sns_envelope.get("MessageId"),
                    "topic_arn": sns_envelope.get("TopicArn"),
                    "subject": sns_envelope.get("Subject"),
                    "published_at": sns_envelope.get("Timestamp"),
                    "alarm": {
                        "name": sns_payload.get("AlarmName") if isinstance(sns_payload, dict) else None,
                        "new_state": sns_payload.get("NewStateValue") if isinstance(sns_payload, dict) else None,
                        "reason": sns_payload.get("NewStateReason") if isinstance(sns_payload, dict) else None,
                        "state_changed_at": sns_payload.get("StateChangeTime") if isinstance(sns_payload, dict) else None,
                        "description": sns_payload.get("AlarmDescription") if isinstance(sns_payload, dict) else None,
                    },
                    "payload": sns_payload,
                }

                notifications.append(notification)

                if delete_on_read and message.get("ReceiptHandle"):
                    self.sqs_client.delete_message(
                        QueueUrl=queue_url,
                        ReceiptHandle=message["ReceiptHandle"],
                    )
                    deleted_count += 1

            return {
                "queue_url": queue_url,
                "count": len(notifications),
                "deleted_count": deleted_count,
                "notifications": notifications,
            }

        except ClientError as e:
            error_msg = f"Failed to poll alarm notifications from SQS: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def delete_alarm_notification(self, queue_url: str, receipt_handle: str) -> Dict:
        """Delete a single SQS alarm notification by receipt handle."""
        try:
            self.sqs_client.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
            )
            return {
                "queue_url": queue_url,
                "deleted": True,
            }
        except ClientError as e:
            error_msg = f"Failed to delete SQS alarm notification: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    @staticmethod
    def _safe_json_load(value: str):
        try:
            return json.loads(value)
        except Exception:
            return value

    def _fetch_metric_series(
        self,
        namespace: str,
        instance_id: str,
        start_time: datetime,
        end_time: datetime,
        period_seconds: int,
        metric_specs: List,
        use_unit_filter: bool = True,
        allow_dimension_fallback: bool = False,
    ) -> Dict:
        metrics = {}

        for metric_name, unit, statistic in metric_specs:
            base_dimensions = [{"Name": "InstanceId", "Value": instance_id}]
            datapoints = self._query_metric_datapoints(
                namespace=namespace,
                metric_name=metric_name,
                dimensions=base_dimensions,
                start_time=start_time,
                end_time=end_time,
                period_seconds=period_seconds,
                statistic=statistic,
                unit=unit if use_unit_filter else None,
            )

            if not datapoints and allow_dimension_fallback:
                discovered_dimensions = self._discover_metric_dimensions(
                    namespace=namespace,
                    metric_name=metric_name,
                    instance_id=instance_id,
                )

                merged_points = []
                for dimensions in discovered_dimensions:
                    # Avoid re-querying the base dimensions set.
                    if self._normalize_dimensions(dimensions) == self._normalize_dimensions(base_dimensions):
                        continue

                    merged_points.extend(
                        self._query_metric_datapoints(
                            namespace=namespace,
                            metric_name=metric_name,
                            dimensions=dimensions,
                            start_time=start_time,
                            end_time=end_time,
                            period_seconds=period_seconds,
                            statistic=statistic,
                            unit=None,
                        )
                    )

                if merged_points:
                    # Keep one datapoint per timestamp (latest value wins on collision).
                    deduped = {dp["Timestamp"]: dp for dp in merged_points}
                    datapoints = sorted(deduped.values(), key=lambda x: x["Timestamp"])

            metrics[metric_name] = {
                "statistic": statistic,
                "unit": unit,
                "datapoints": [
                    {
                        "timestamp": dp["Timestamp"].isoformat(),
                        "value": dp.get(statistic),
                    }
                    for dp in datapoints
                ],
            }

        return metrics

    def _query_metric_datapoints(
        self,
        namespace: str,
        metric_name: str,
        dimensions: List[Dict[str, str]],
        start_time: datetime,
        end_time: datetime,
        period_seconds: int,
        statistic: str,
        unit: Optional[str] = None,
    ) -> List[Dict]:
        params = {
            "Namespace": namespace,
            "MetricName": metric_name,
            "Dimensions": dimensions,
            "StartTime": start_time,
            "EndTime": end_time,
            "Period": period_seconds,
            "Statistics": [statistic],
        }

        if unit:
            params["Unit"] = unit

        response = self.cloudwatch_client.get_metric_statistics(**params)
        return response.get("Datapoints", [])

    def _discover_metric_dimensions(
        self,
        namespace: str,
        metric_name: str,
        instance_id: str,
    ) -> List[List[Dict[str, str]]]:
        dimensions = []
        next_token = None

        while True:
            params = {
                "Namespace": namespace,
                "MetricName": metric_name,
                "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
            }
            if next_token:
                params["NextToken"] = next_token

            response = self.cloudwatch_client.list_metrics(**params)
            for metric in response.get("Metrics", []):
                metric_dimensions = metric.get("Dimensions", [])
                if metric_dimensions:
                    dimensions.append(metric_dimensions)

            next_token = response.get("NextToken")
            if not next_token:
                break

        return dimensions

    @staticmethod
    def _normalize_dimensions(dimensions: List[Dict[str, str]]) -> List[tuple]:
        return sorted(
            [(d.get("Name"), d.get("Value")) for d in dimensions],
            key=lambda item: (item[0] or "", item[1] or ""),
        )

    def _summarize_metric_availability(self, metrics: Dict) -> Dict:
        total_metrics = len(metrics)
        metrics_with_data = 0
        metrics_without_data = []
        total_datapoints = 0

        for metric_name, metric_payload in metrics.items():
            datapoints = metric_payload.get("datapoints", [])
            datapoint_count = len(datapoints)
            total_datapoints += datapoint_count

            if datapoint_count > 0:
                metrics_with_data += 1
            else:
                metrics_without_data.append(metric_name)

        return {
            "total_metrics": total_metrics,
            "metrics_with_data": metrics_with_data,
            "metrics_without_data": metrics_without_data,
            "total_datapoints": total_datapoints,
        }

    def list_log_groups(self, prefix: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """
        List CloudWatch log groups.

        Args:
            prefix: Optional log group name prefix
            limit: Maximum number of results

        Returns:
            List of log group summaries
        """
        try:
            params = {"limit": limit}
            if prefix:
                params["logGroupNamePrefix"] = prefix

            response = self.logs_client.describe_log_groups(**params)
            groups = []
            for group in response.get("logGroups", []):
                groups.append(
                    {
                        "log_group_name": group.get("logGroupName"),
                        "retention_in_days": group.get("retentionInDays"),
                        "stored_bytes": group.get("storedBytes", 0),
                        "metric_filter_count": group.get("metricFilterCount", 0),
                    }
                )

            return groups

        except ClientError as e:
            error_msg = f"Failed to list log groups: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def list_log_streams(self, log_group_name: str, limit: int = 25, descending: bool = True) -> List[Dict]:
        """
        List log streams for a CloudWatch log group.

        Args:
            log_group_name: Log group name
            limit: Maximum number of streams
            descending: Sort newest first by LastEventTime

        Returns:
            List of log stream summaries
        """
        try:
            response = self.logs_client.describe_log_streams(
                logGroupName=log_group_name,
                orderBy="LastEventTime",
                descending=descending,
                limit=limit,
            )

            streams = []
            for stream in response.get("logStreams", []):
                last_event = stream.get("lastEventTimestamp")
                last_event_iso = None
                if last_event:
                    last_event_iso = datetime.fromtimestamp(last_event / 1000, tz=timezone.utc).isoformat()

                streams.append(
                    {
                        "log_stream_name": stream.get("logStreamName"),
                        "stored_bytes": stream.get("storedBytes", 0),
                        "last_event_timestamp": last_event_iso,
                    }
                )

            return streams

        except ClientError as e:
            error_msg = f"Failed to list log streams: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def get_log_events(
        self,
        log_group_name: str,
        log_stream_name: str,
        limit: int = 100,
        start_from_head: bool = False,
    ) -> Dict:
        """
        Get events from a specific log stream.

        Args:
            log_group_name: Log group name
            log_stream_name: Log stream name
            limit: Max events
            start_from_head: If True, reads oldest first

        Returns:
            Dictionary with log events
        """
        try:
            response = self.logs_client.get_log_events(
                logGroupName=log_group_name,
                logStreamName=log_stream_name,
                limit=limit,
                startFromHead=start_from_head,
            )

            events = [
                {
                    "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
                    "message": e.get("message", ""),
                }
                for e in response.get("events", [])
            ]

            return {
                "log_group_name": log_group_name,
                "log_stream_name": log_stream_name,
                "count": len(events),
                "events": events,
            }

        except ClientError as e:
            error_msg = f"Failed to get log events: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def filter_logs(
        self,
        log_group_name: str,
        filter_pattern: str = "",
        minutes: int = 15,
        limit: int = 100,
    ) -> Dict:
        """
        Filter log events in a log group over a recent time window.

        Args:
            log_group_name: Log group name
            filter_pattern: CloudWatch Logs filter pattern
            minutes: Lookback window in minutes
            limit: Max events returned

        Returns:
            Dictionary with matched events
        """
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=minutes)

            response = self.logs_client.filter_log_events(
                logGroupName=log_group_name,
                filterPattern=filter_pattern,
                startTime=int(start_time.timestamp() * 1000),
                endTime=int(end_time.timestamp() * 1000),
                limit=limit,
            )

            events = [
                {
                    "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
                    "log_stream_name": e.get("logStreamName"),
                    "message": e.get("message", ""),
                }
                for e in response.get("events", [])
            ]

            return {
                "log_group_name": log_group_name,
                "filter_pattern": filter_pattern,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "count": len(events),
                "events": events,
            }

        except ClientError as e:
            error_msg = f"Failed to filter logs: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def list_alarms(
        self,
        state_value: Optional[str] = None,
        alarm_name_prefix: Optional[str] = None,
        max_records: int = 100,
    ) -> List[Dict]:
        """
        List CloudWatch metric alarms.

        Args:
            state_value: Optional state filter (OK | ALARM | INSUFFICIENT_DATA)
            alarm_name_prefix: Optional alarm name prefix
            max_records: Max alarms to return

        Returns:
            List of alarm summaries
        """
        try:
            params = {"MaxRecords": max_records}
            if state_value:
                params["StateValue"] = state_value
            if alarm_name_prefix:
                params["AlarmNamePrefix"] = alarm_name_prefix

            response = self.cloudwatch_client.describe_alarms(**params)
            alarms = []
            for alarm in response.get("MetricAlarms", []):
                dimensions = alarm.get("Dimensions", [])
                alarms.append(
                    {
                        "alarm_name": alarm.get("AlarmName"),
                        "alarm_description": alarm.get("AlarmDescription"),
                        "state_value": alarm.get("StateValue"),
                        "state_reason": alarm.get("StateReason"),
                        "metric_name": alarm.get("MetricName"),
                        "namespace": alarm.get("Namespace"),
                        "dimensions": [
                            {
                                "name": dim.get("Name"),
                                "value": dim.get("Value"),
                            }
                            for dim in dimensions
                        ],
                        "comparison_operator": alarm.get("ComparisonOperator"),
                        "threshold": alarm.get("Threshold"),
                        "period": alarm.get("Period"),
                        "evaluation_periods": alarm.get("EvaluationPeriods"),
                        "alarm_actions": alarm.get("AlarmActions", []),
                    }
                )

            return alarms

        except ClientError as e:
            error_msg = f"Failed to list alarms: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def create_metric_alarm(
        self,
        alarm_name: str,
        metric_name: str,
        namespace: str,
        threshold: float,
        comparison_operator: str,
        evaluation_periods: int,
        period: int,
        statistic: str = "Average",
        dimensions: Optional[List[Dict[str, str]]] = None,
        alarm_actions: Optional[List[str]] = None,
        ok_actions: Optional[List[str]] = None,
        treat_missing_data: str = "missing",
        alarm_description: Optional[str] = None,
    ) -> Dict:
        """
        Create or update a CloudWatch metric alarm.

        Returns:
            Dictionary with alarm status
        """
        try:
            params = {
                "AlarmName": alarm_name,
                "MetricName": metric_name,
                "Namespace": namespace,
                "Threshold": threshold,
                "ComparisonOperator": comparison_operator,
                "EvaluationPeriods": evaluation_periods,
                "Period": period,
                "Statistic": statistic,
                "TreatMissingData": treat_missing_data,
            }

            if dimensions:
                params["Dimensions"] = dimensions
            if alarm_actions:
                params["AlarmActions"] = alarm_actions
            if ok_actions:
                params["OKActions"] = ok_actions
            if alarm_description:
                params["AlarmDescription"] = alarm_description

            self.cloudwatch_client.put_metric_alarm(**params)

            return {
                "alarm_name": alarm_name,
                "status": "created_or_updated",
            }

        except ClientError as e:
            error_msg = f"Failed to create metric alarm: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def get_dashboard(self, dashboard_name: str) -> Dict:
        """
        Get a CloudWatch dashboard body.

        Args:
            dashboard_name: Dashboard name

        Returns:
            Dashboard metadata and body
        """
        try:
            response = self.cloudwatch_client.get_dashboard(DashboardName=dashboard_name)
            return {
                "dashboard_name": dashboard_name,
                "dashboard_arn": response.get("DashboardArn"),
                "dashboard_body": response.get("DashboardBody", ""),
            }
        except ClientError as e:
            error_msg = f"Failed to get dashboard: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
