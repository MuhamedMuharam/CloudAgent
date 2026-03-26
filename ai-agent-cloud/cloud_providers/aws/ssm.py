"""
AWS SSM Manager
Handles AWS Systems Manager Run Command operations for remote EC2 service control.
"""

import time
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError


class SSMManager:
    """Manages SSM Run Command operations for EC2 instances."""

    def __init__(self, region: str = 'us-east-1'):
        """
        Initialize SSM Manager.

        Args:
            region: AWS region to operate in
        """
        self.region = region
        self.ssm_client = boto3.client('ssm', region_name=region)

    def run_command(
        self,
        instance_ids: List[str],
        commands: List[str],
        comment: Optional[str] = None,
        timeout_seconds: int = 600,
        working_directory: Optional[str] = None,
        wait_for_completion: bool = False,
        completion_timeout_seconds: int = 60,
        poll_interval_seconds: int = 2,
    ) -> Dict:
        """
        Execute shell commands on EC2 instances through SSM Run Command.

        Args:
            instance_ids: Target EC2 instance IDs
            commands: Shell commands to execute
            comment: Optional command description
            timeout_seconds: Maximum execution time in seconds
            working_directory: Optional working directory on target host
            wait_for_completion: Wait for command execution to finish and include outputs
            completion_timeout_seconds: Maximum time to wait for terminal status
            poll_interval_seconds: Polling interval while waiting

        Returns:
            Dictionary with command metadata and target IDs
        """
        try:
            parameters = {
                'commands': commands,
                'executionTimeout': [str(timeout_seconds)],
            }
            if working_directory:
                parameters['workingDirectory'] = [working_directory]

            response = self.ssm_client.send_command(
                InstanceIds=instance_ids,
                DocumentName='AWS-RunShellScript',
                Parameters=parameters,
                Comment=comment or 'AI Agent SSM command',
            )

            command = response.get('Command', {})
            if wait_for_completion:
                command_id = command.get('CommandId')
                invocations = self.wait_for_command_completion(
                    command_id=command_id,
                    instance_ids=instance_ids,
                    completion_timeout_seconds=completion_timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )
                final_status = self._aggregate_invocation_status(invocations)

                return {
                    'command_id': command_id,
                    'status': final_status,
                    'document_name': command.get('DocumentName'),
                    'requested_date_time': str(command.get('RequestedDateTime')),
                    'instance_ids': instance_ids,
                    'commands': commands,
                    'waited_for_completion': True,
                    'completion_timeout_seconds': completion_timeout_seconds,
                    'invocations': invocations,
                }

            return {
                'command_id': command.get('CommandId'),
                'status': command.get('Status'),
                'document_name': command.get('DocumentName'),
                'requested_date_time': str(command.get('RequestedDateTime')),
                'instance_ids': instance_ids,
                'commands': commands,
                'waited_for_completion': False,
            }

        except ClientError as e:
            raise Exception(f"Failed to run SSM command: {e}")

    def get_command_output(self, command_id: str, instance_id: str) -> Dict:
        """
        Fetch execution output for a previously issued Run Command.

        Args:
            command_id: SSM command ID
            instance_id: Target instance ID

        Returns:
            Command invocation status and output streams
        """
        try:
            response = self.ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )

            return {
                'command_id': command_id,
                'instance_id': instance_id,
                'status': response.get('Status'),
                'status_details': response.get('StatusDetails'),
                'response_code': response.get('ResponseCode'),
                'stdout': response.get('StandardOutputContent', ''),
                'stderr': response.get('StandardErrorContent', ''),
                'execution_start_date_time': response.get('ExecutionStartDateTime', ''),
                'execution_end_date_time': response.get('ExecutionEndDateTime', ''),
            }

        except ClientError as e:
            raise Exception(f"Failed to get SSM command output: {e}")

    def wait_for_command_completion(
        self,
        command_id: str,
        instance_ids: List[str],
        completion_timeout_seconds: int = 60,
        poll_interval_seconds: int = 2,
    ) -> List[Dict]:
        """
        Poll command invocation status until all targets reach terminal state or timeout.

        Returns:
            List of per-instance invocation outputs
        """
        deadline = time.time() + max(completion_timeout_seconds, 1)
        poll_interval_seconds = max(poll_interval_seconds, 1)

        latest_outputs = {
            instance_id: {
                'command_id': command_id,
                'instance_id': instance_id,
                'status': 'Pending',
                'status_details': 'Pending',
                'response_code': None,
                'stdout': '',
                'stderr': '',
                'execution_start_date_time': '',
                'execution_end_date_time': '',
                'timed_out': False,
            }
            for instance_id in instance_ids
        }

        terminal_states = {'Success', 'Failed', 'Cancelled', 'TimedOut', 'Cancelling'}

        while time.time() < deadline:
            all_terminal = True
            for instance_id in instance_ids:
                try:
                    output = self.get_command_output(command_id=command_id, instance_id=instance_id)
                    latest_outputs[instance_id] = output
                    if output.get('status') not in terminal_states:
                        all_terminal = False
                except Exception as error:
                    all_terminal = False
                    latest_outputs[instance_id] = {
                        'command_id': command_id,
                        'instance_id': instance_id,
                        'status': 'Pending',
                        'status_details': 'InvocationNotReady',
                        'response_code': None,
                        'stdout': '',
                        'stderr': str(error),
                        'execution_start_date_time': '',
                        'execution_end_date_time': '',
                        'timed_out': False,
                    }

            if all_terminal:
                return [latest_outputs[instance_id] for instance_id in instance_ids]

            time.sleep(poll_interval_seconds)

        for instance_id in instance_ids:
            latest_outputs[instance_id]['timed_out'] = True
            if latest_outputs[instance_id].get('status') not in terminal_states:
                latest_outputs[instance_id]['status'] = 'TimedOut'
                latest_outputs[instance_id]['status_details'] = 'ClientWaitTimedOut'

        return [latest_outputs[instance_id] for instance_id in instance_ids]

    def _aggregate_invocation_status(self, invocations: List[Dict]) -> str:
        """Return an aggregate status across all invocation results."""
        statuses = [inv.get('status', 'Unknown') for inv in invocations]
        if not statuses:
            return 'Unknown'
        if all(status == 'Success' for status in statuses):
            return 'Success'
        if any(status in {'Failed', 'Cancelled', 'TimedOut', 'Cancelling'} for status in statuses):
            return 'Failed'
        if any(status in {'InProgress', 'Pending', 'Delayed'} for status in statuses):
            return 'InProgress'
        return statuses[0]

    def start_service(
        self,
        instance_ids: List[str],
        service_name: str,
        wait_for_completion: bool = True,
        completion_timeout_seconds: int = 60,
        poll_interval_seconds: int = 2,
    ) -> Dict:
        """Start a systemd service on target instances."""
        return self.run_command(
            instance_ids=instance_ids,
            commands=[f"sudo systemctl start {service_name}"],
            comment=f"Start service: {service_name}",
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def stop_service(
        self,
        instance_ids: List[str],
        service_name: str,
        wait_for_completion: bool = True,
        completion_timeout_seconds: int = 60,
        poll_interval_seconds: int = 2,
    ) -> Dict:
        """Stop a systemd service on target instances."""
        return self.run_command(
            instance_ids=instance_ids,
            commands=[f"sudo systemctl stop {service_name}"],
            comment=f"Stop service: {service_name}",
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def restart_service(
        self,
        instance_ids: List[str],
        service_name: str,
        wait_for_completion: bool = True,
        completion_timeout_seconds: int = 60,
        poll_interval_seconds: int = 2,
    ) -> Dict:
        """Restart a systemd service on target instances."""
        return self.run_command(
            instance_ids=instance_ids,
            commands=[f"sudo systemctl restart {service_name}"],
            comment=f"Restart service: {service_name}",
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def get_service_status(
        self,
        instance_ids: List[str],
        service_name: str,
        wait_for_completion: bool = True,
        completion_timeout_seconds: int = 60,
        poll_interval_seconds: int = 2,
    ) -> Dict:
        """Get a concise active state for a systemd service on target instances."""
        return self.run_command(
            instance_ids=instance_ids,
            commands=[f"systemctl is-active {service_name}"],
            comment=f"Get service status: {service_name}",
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def list_running_services(
        self,
        instance_id: str,
        wait_for_completion: bool = True,
        completion_timeout_seconds: int = 60,
        poll_interval_seconds: int = 2,
    ) -> Dict:
        """List running systemd services on one instance."""
        return self.run_command(
            instance_ids=[instance_id],
            commands=["systemctl list-units --type=service --state=running --no-pager"],
            comment='List running services',
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
