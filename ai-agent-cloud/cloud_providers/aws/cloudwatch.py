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
            # CloudWatch DescribeAlarms accepts MaxRecords in range [1, 100].
            safe_max_records = max(1, min(int(max_records), 100))
            params = {"MaxRecords": safe_max_records}
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
                        "state_updated_timestamp": (
                            alarm.get("StateUpdatedTimestamp").isoformat()
                            if alarm.get("StateUpdatedTimestamp")
                            else None
                        ),
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

    def analyze_ec2_rightsizing(
        self,
        instance_id: str,
        minutes: int = 180,
        period_seconds: int = 300,
        cpu_idle_threshold_percent: float = 15.0,
        cpu_hot_threshold_percent: float = 70.0,
        network_idle_threshold_bytes_per_period: float = 50000.0,
        network_idle_threshold_bytes_per_second: Optional[float] = None,
        cpu_peak_cap_percent: float = 50.0,
        include_extended_signals: bool = True,
        memory_pressure_threshold_percent: float = 75.0,
        disk_pressure_threshold_percent: float = 80.0,
        swap_pressure_threshold_percent: float = 50.0,
    ) -> Dict:
        """
        Analyze EC2 utilization to produce a rightsizing recommendation signal.
        """
        metrics_payload = self.get_ec2_metrics(
            instance_id=instance_id,
            minutes=minutes,
            period_seconds=period_seconds,
            include_agent_metrics=include_extended_signals,
        )
        metrics = metrics_payload.get('metrics', {})
        agent_metrics = metrics_payload.get('agent_metrics', {})

        cpu_values = self._extract_metric_values(metrics, 'CPUUtilization')
        network_in_values = self._extract_metric_values(metrics, 'NetworkIn')
        network_out_values = self._extract_metric_values(metrics, 'NetworkOut')
        memory_values = self._extract_metric_values(agent_metrics, 'mem_used_percent')
        disk_values = self._extract_metric_values(agent_metrics, 'disk_used_percent')
        swap_values = self._extract_metric_values(agent_metrics, 'swap_used_percent')

        cpu_avg = self._safe_avg(cpu_values)
        cpu_max = max(cpu_values) if cpu_values else None
        network_in_avg = self._safe_avg(network_in_values)
        network_out_avg = self._safe_avg(network_out_values)
        memory_avg = self._safe_avg(memory_values)
        memory_max = max(memory_values) if memory_values else None
        disk_avg = self._safe_avg(disk_values)
        disk_max = max(disk_values) if disk_values else None
        swap_avg = self._safe_avg(swap_values)
        swap_max = max(swap_values) if swap_values else None
        network_total_avg = None
        if network_in_avg is not None and network_out_avg is not None:
            network_total_avg = network_in_avg + network_out_avg

        # Normalize network threshold to the actual metric period so the value stays
        # meaningful regardless of what period the adaptive resolver picked.
        effective_network_threshold = (
            network_idle_threshold_bytes_per_second * period_seconds
            if network_idle_threshold_bytes_per_second is not None
            else network_idle_threshold_bytes_per_period
        )

        recommendation = 'investigate'
        reason = 'Insufficient signal to classify utilization trend.'

        # CPU is the primary signal; network is a secondary veto only when it clearly
        # exceeds background agent noise (3× the floor). This prevents false "keep"
        # results on idle instances where SSM/CW-Agent traffic alone sits above the floor.
        NETWORK_VETO_MULTIPLIER = 3

        if cpu_avg is not None:
            if cpu_avg >= cpu_hot_threshold_percent:
                recommendation = 'upsize'
                reason = f"High utilization detected: avg CPU {cpu_avg:.2f}%"
            elif cpu_avg <= cpu_idle_threshold_percent:
                network_veto_threshold = effective_network_threshold * NETWORK_VETO_MULTIPLIER
                if network_total_avg is not None and network_total_avg > network_veto_threshold:
                    recommendation = 'keep'
                    reason = (
                        f"CPU is idle ({cpu_avg:.2f}%) but network activity "
                        f"({network_total_avg:.0f} b/period) exceeds background noise floor "
                        f"({network_veto_threshold:.0f} b/period) — likely active workload"
                    )
                else:
                    recommendation = 'downsize'
                    network_note = (
                        f", network {network_total_avg:.0f} b/period is within background range"
                        if network_total_avg is not None
                        else ""
                    )
                    reason = f"Low utilization: avg CPU {cpu_avg:.2f}%{network_note}"
            else:
                recommendation = 'keep'
                reason = f"Utilization appears balanced: avg CPU {cpu_avg:.2f}%"

        extended_signal_findings = []
        if include_extended_signals:
            if memory_max is not None and memory_max >= memory_pressure_threshold_percent:
                extended_signal_findings.append(
                    f"Memory pressure detected (max mem_used_percent {memory_max:.2f}% >= {memory_pressure_threshold_percent:.2f}%)"
                )
            if disk_max is not None and disk_max >= disk_pressure_threshold_percent:
                extended_signal_findings.append(
                    f"Disk pressure detected (max disk_used_percent {disk_max:.2f}% >= {disk_pressure_threshold_percent:.2f}%)"
                )
            if swap_max is not None and swap_max >= swap_pressure_threshold_percent:
                extended_signal_findings.append(
                    f"Swap pressure detected (max swap_used_percent {swap_max:.2f}% >= {swap_pressure_threshold_percent:.2f}%)"
                )

            # Use extended metrics as a safety guardrail when available.
            if extended_signal_findings and recommendation in {'downsize', 'keep'}:
                recommendation = 'investigate'
                reason = (
                    "Baseline CPU/network suggested non-critical utilization, but extended memory/disk/swap signals indicate pressure. "
                    "Review instance sizing and host storage before applying cost reduction."
                )

        # Protect bursty workloads: avg can be low while peak is high (e.g. batch jobs, web spikes).
        if recommendation == 'downsize' and cpu_max is not None and cpu_max > cpu_peak_cap_percent:
            recommendation = 'investigate'
            reason = (
                f"Avg CPU ({cpu_avg:.2f}%) is below the idle threshold but peak CPU ({cpu_max:.2f}%) "
                f"exceeds the burst cap ({cpu_peak_cap_percent:.2f}%). The instance likely has a "
                f"bursty workload — review the traffic pattern before downsizing."
            )

        return {
            'instance_id': instance_id,
            'window_minutes': minutes,
            'period_seconds': period_seconds,
            'metrics_summary': {
                'cpu_avg_percent': cpu_avg,
                'cpu_max_percent': cpu_max,
                'network_in_avg_bytes_per_period': network_in_avg,
                'network_out_avg_bytes_per_period': network_out_avg,
                'network_total_avg_bytes_per_period': network_total_avg,
                'memory_avg_percent': memory_avg,
                'memory_max_percent': memory_max,
                'disk_avg_percent': disk_avg,
                'disk_max_percent': disk_max,
                'swap_avg_percent': swap_avg,
                'swap_max_percent': swap_max,
                'datapoint_count': {
                    'cpu': len(cpu_values),
                    'network_in': len(network_in_values),
                    'network_out': len(network_out_values),
                    'memory': len(memory_values),
                    'disk': len(disk_values),
                    'swap': len(swap_values),
                },
            },
            'thresholds': {
                'cpu_idle_threshold_percent': cpu_idle_threshold_percent,
                'cpu_hot_threshold_percent': cpu_hot_threshold_percent,
                'cpu_peak_cap_percent': cpu_peak_cap_percent,
                'network_idle_threshold_bytes_per_second': network_idle_threshold_bytes_per_second,
                'effective_network_idle_threshold_bytes_per_period': effective_network_threshold,
                'memory_pressure_threshold_percent': memory_pressure_threshold_percent,
                'disk_pressure_threshold_percent': disk_pressure_threshold_percent,
                'swap_pressure_threshold_percent': swap_pressure_threshold_percent,
            },
            'analysis_basis': {
                'primary_signals': ['CPUUtilization', 'NetworkIn', 'NetworkOut'],
                'extended_signals_enabled': include_extended_signals,
                'extended_signals_considered': [
                    name
                    for name, points in [
                        ('mem_used_percent', len(memory_values)),
                        ('disk_used_percent', len(disk_values)),
                        ('swap_used_percent', len(swap_values)),
                    ]
                    if points > 0
                ],
                'extended_signals_missing': [
                    name
                    for name, points in [
                        ('mem_used_percent', len(memory_values)),
                        ('disk_used_percent', len(disk_values)),
                        ('swap_used_percent', len(swap_values)),
                    ]
                    if points == 0
                ]
                if include_extended_signals
                else [],
            },
            'extended_signal_findings': extended_signal_findings,
            'recommendation': recommendation,
            'reason': reason,
        }

    @staticmethod
    def _extract_metric_values(metrics_payload: Dict, metric_name: str) -> List[float]:
        metric_item = metrics_payload.get(metric_name, {}) if isinstance(metrics_payload, dict) else {}
        datapoints = metric_item.get('datapoints', []) if isinstance(metric_item, dict) else []
        values = []
        for dp in datapoints:
            value = dp.get('value')
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    @staticmethod
    def _safe_avg(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return sum(values) / len(values)
