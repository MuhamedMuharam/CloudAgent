"""
AWS CloudWatch Manager
Handles CloudWatch Metrics, Logs, Alarms, and Dashboards.
"""

import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError


class CloudWatchManager:
    """Manages AWS CloudWatch observability operations."""

    def __init__(self, region: str = "us-east-1"):
        """
        Initialize CloudWatch Manager.

        Args:
            region: AWS region (default: us-east-1)
        """
        self.region = region
        self.cloudwatch_client = boto3.client("cloudwatch", region_name=region)
        self.logs_client = boto3.client("logs", region_name=region)

    def get_ec2_metrics(
        self,
        instance_id: str,
        minutes: int = 15,
        period_seconds: int = 60,
        namespace: str = "AWS/EC2",
    ) -> Dict:
        """
        Get key EC2 CloudWatch metrics for an instance.

        Args:
            instance_id: EC2 instance ID
            minutes: Time window to query in minutes
            period_seconds: Metrics period in seconds
            namespace: CloudWatch namespace

        Returns:
            Dictionary with metric datapoints
        """
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=minutes)

        metric_specs = [
            ("CPUUtilization", "Percent", "Average"),
            ("NetworkIn", "Bytes", "Sum"),
            ("NetworkOut", "Bytes", "Sum"),
            ("StatusCheckFailed", "Count", "Maximum"),
            ("StatusCheckFailed_Instance", "Count", "Maximum"),
            ("StatusCheckFailed_System", "Count", "Maximum"),
        ]

        metrics = {}
        try:
            for metric_name, unit, statistic in metric_specs:
                response = self.cloudwatch_client.get_metric_statistics(
                    Namespace=namespace,
                    MetricName=metric_name,
                    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=period_seconds,
                    Statistics=[statistic],
                    Unit=unit,
                )

                datapoints = sorted(response.get("Datapoints", []), key=lambda x: x["Timestamp"])
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

            return {
                "instance_id": instance_id,
                "namespace": namespace,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "period_seconds": period_seconds,
                "metrics": metrics,
            }

        except ClientError as e:
            error_msg = f"Failed to fetch EC2 metrics: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

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
