"""
Policy Engine
Validates AI agent actions against security and compliance policies.

This is a critical safety component that prevents the agent from:
- Creating expensive resources
- Opening dangerous security group rules
- Violating organizational policies
"""

import yaml
import re
import ipaddress
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple
from cloud_providers.aws.mapping import map_generic_to_instance_type, get_estimated_hourly_cost


class PolicyViolation(Exception):
    """Raised when an action violates a policy."""
    pass


class PolicyEngine:
    """
    Validates agent actions against defined policies.
    
    Policies are defined in YAML files and checked BEFORE
    executing any cloud provider API calls.
    """
    
    def __init__(self, policy_file: str = "policies/aws_policies.yaml"):
        """
        Initialize policy engine.
        
        Args:
            policy_file: Path to YAML policy file
        """
        self.policy_file = Path(policy_file)
        self.policies = self._load_policies()
        self.pending_recommendations: List[Dict[str, Any]] = []
        
        if self.policies:
            print(f"✅ Policy engine loaded with {len(self.policies)} policy sections")
        else:
            print("⚠️  No policies loaded - running without policy enforcement")

    DEFAULT_BLOCKED_SSM_COMMAND_PATTERNS = [
        r"\brm\s+-rf\s+/(?:\s|$)",
        r"\bmkfs(?:\.[a-z0-9]+)?\b",
        r"\bdd\s+if=",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bpoweroff\b",
        r"\bhalt\b",
        r"\binit\s+0\b",
        r"\b(?:apt|apt-get|yum|dnf|zypper|pacman)\b.*\b(?:remove|purge|erase|autoremove)\b",
        r"\b(?:apt|apt-get)\b.*\b(?:dist-upgrade|full-upgrade)\b",
        r"\bcurl\b.*\|\s*(?:bash|sh)\b",
        r"\bwget\b.*\|\s*(?:bash|sh)\b",
    ]

    DEFAULT_BLOCKED_SSM_SERVICES = {
        "sshd",
        "network",
        "networkmanager",
        "systemd-networkd",
        "firewalld",
        "dbus",
    }

    SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{1,128}(?:\.service)?$")
    
    def _load_policies(self) -> Dict:
        """Load policies from YAML file."""
        if not self.policy_file.exists():
            print(f"⚠️  Policy file not found: {self.policy_file}")
            return {}
        
        try:
            with open(self.policy_file) as f:
                policies = yaml.safe_load(f)
                return policies if policies else {}
        except Exception as e:
            print(f"⚠️  Error loading policies: {e}")
            return {}
    
    def validate_action(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        """
        Validate an action against policies.
        
        Args:
            tool_name: Name of the MCP tool being called
            arguments: Arguments being passed to the tool
        
        Raises:
            PolicyViolation: If action violates any policy
        """
        if not self.policies:
            return  # No policies to enforce
        
        # Route to appropriate validator based on tool name
        if tool_name == "aws_create_ec2_instance":
            self._validate_ec2_creation(arguments)
        elif tool_name == "aws_create_vpc":
            self._validate_vpc_creation(arguments)
        elif tool_name == "aws_create_security_group":
            self._validate_security_group_creation(arguments)
        elif tool_name == "aws_add_security_group_rule":
            self._validate_security_group_rule(arguments)
        elif tool_name == "aws_edit_security_group_rule":
            # Validate the replacement rule before allowing update.
            self._validate_security_group_rule({"rule": arguments.get("new_rule", {})})
        elif tool_name == "aws_remove_security_group_rule":
            # Removing rules is allowed by default.
            pass
        elif tool_name == "aws_create_nat_gateway":
            self._validate_nat_gateway_creation(arguments)
        elif tool_name in {"aws_resize_ec2_instance", "aws_apply_ec2_rightsizing"}:
            self._validate_ec2_resize(arguments)
        elif tool_name == "aws_create_metric_alarm":
            self._validate_alarm_creation(arguments)
        elif tool_name == "aws_ssm_run_command":
            self._validate_ssm_run_command(arguments)
        elif tool_name == "aws_ssm_collect_host_diagnostics":
            self._validate_ssm_collect_host_diagnostics(arguments)
        elif tool_name == "aws_ssm_safe_disk_cleanup":
            self._validate_ssm_safe_disk_cleanup(arguments)
        elif tool_name in {
            "aws_ssm_start_service",
            "aws_ssm_stop_service",
            "aws_ssm_restart_service",
            "aws_ssm_get_service_status",
            "aws_ssm_list_running_services",
        }:
            self._validate_ssm_service_operation(tool_name, arguments)
        # Add more validators as needed

    def pop_policy_recommendations(self) -> List[Dict[str, Any]]:
        """Return and clear pending non-blocking policy recommendations."""
        buffered = list(self.pending_recommendations)
        self.pending_recommendations.clear()
        return buffered

    def _policy_mode(self) -> str:
        """Resolve policy mode for cost optimization checks."""
        cost_cfg = self.policies.get('cost_optimization', {})
        mode = str(cost_cfg.get('policy_mode', 'recommend')).strip().lower()
        if mode not in {'recommend', 'enforce'}:
            mode = 'recommend'
        return mode

    def _emit_cost_violation_or_recommendation(self, message: str, code: str):
        """Block in enforce mode, otherwise keep execution and emit recommendation."""
        mode = self._policy_mode()
        if mode == 'enforce':
            raise PolicyViolation(message)

        recommendation = {
            'domain': 'cost_optimization',
            'code': code,
            'message': message,
            'mode': mode,
        }
        self.pending_recommendations.append(recommendation)
        print(f"⚠️  COST POLICY RECOMMENDATION: {message}")
    
    def _validate_ec2_creation(self, args: Dict[str, Any]):
        """Validate EC2 instance creation."""
        ec2_policies = self.policies.get('ec2', {})
        
        # Check CPU limit
        max_cpu = ec2_policies.get('max_cpu', 999)
        cpu = args.get('cpu', 2)
        if cpu > max_cpu:
            raise PolicyViolation(
                f"CPU count {cpu} exceeds maximum allowed {max_cpu}. "
                f"Policy prevents creating expensive instances."
            )
        
        # Check RAM limit
        max_ram = ec2_policies.get('max_ram_gb', 999)
        ram = args.get('ram', 4)
        if ram > max_ram:
            raise PolicyViolation(
                f"RAM {ram}GB exceeds maximum allowed {max_ram}GB. "
                f"Policy prevents creating expensive instances."
            )

        # Required tags for governance and cost attribution.
        required_tags = [str(tag).strip() for tag in ec2_policies.get('required_tags', []) if str(tag).strip()]
        provided_tags = args.get('tags') if isinstance(args.get('tags'), dict) else {}
        missing_tags = [tag for tag in required_tags if tag not in provided_tags and tag != 'ManagedBy']
        if missing_tags:
            self._emit_cost_violation_or_recommendation(
                "Missing required tags for EC2 creation: " + ", ".join(missing_tags),
                code='missing_required_tags',
            )

        # Real-time cost guardrail.
        requested_instance_type = args.get('instance_type')
        if requested_instance_type:
            instance_type = str(requested_instance_type)
        else:
            instance_type = map_generic_to_instance_type(cpu=cpu, ram=ram)

        estimated_hourly_cost = get_estimated_hourly_cost(instance_type)
        max_hourly_cost = float(ec2_policies.get('max_hourly_cost', 9999.0))
        if estimated_hourly_cost > max_hourly_cost:
            self._emit_cost_violation_or_recommendation(
                (
                    f"Estimated hourly cost ${estimated_hourly_cost:.4f} for instance type '{instance_type}' "
                    f"exceeds policy max_hourly_cost ${max_hourly_cost:.4f}."
                ),
                code='ec2_hourly_cost_limit',
            )
        
        print(f"✅ Policy validation passed for EC2 instance '{args.get('name', 'unnamed')}'")
    
    def _validate_vpc_creation(self, args: Dict[str, Any]):
        """Validate VPC creation."""
        vpc_policies = self.policies.get('vpc', {})
        
        # Check CIDR block is in allowed ranges
        cidr_block = args.get('cidr_block', '')
        allowed_cidrs = vpc_policies.get('allowed_cidr_blocks', [])
        
        if allowed_cidrs:
            # Check if CIDR starts with an allowed prefix
            is_allowed = False
            for allowed_cidr in allowed_cidrs:
                # Simple check: does the CIDR start with an allowed prefix?
                allowed_prefix = allowed_cidr.split('/')[0].split('.')[0]
                actual_prefix = cidr_block.split('.')[0]
                if actual_prefix == allowed_prefix:
                    is_allowed = True
                    break
            
            if not is_allowed:
                raise PolicyViolation(
                    f"CIDR block {cidr_block} is not in allowed ranges. "
                    f"Allowed: {', '.join(allowed_cidrs)}"
                )
        
        # Check CIDR block size (AWS requirement: between /16 and /28)
        if '/' in cidr_block:
            prefix_length = int(cidr_block.split('/')[1])
            min_prefix = vpc_policies.get('min_cidr_prefix', 16)
            max_prefix = vpc_policies.get('max_cidr_prefix', 28)
            
            if prefix_length < min_prefix or prefix_length > max_prefix:
                raise PolicyViolation(
                    f"VPC CIDR block size /{prefix_length} is invalid. "
                    f"AWS requires CIDR block size between /{min_prefix} and /{max_prefix}. "
                    f"Example: 10.0.0.0/16 is valid, 10.0.0.0/8 or 10.0.0.0/29 are not."
                )
        
        print(f"✅ Policy validation passed for VPC '{args.get('name', 'unnamed')}'")
    
    def _validate_security_group_creation(self, args: Dict[str, Any]):
        """Validate security group creation."""
        sg_policies = self.policies.get('security_groups', {})
        
        # Check if description is required
        if sg_policies.get('require_description', False):
            if not args.get('description'):
                raise PolicyViolation(
                    "Security group description is required by policy"
                )
        
        # Validate rules if provided
        rules = args.get('rules', [])
        if rules:
            for rule in rules:
                self._validate_single_security_group_rule(rule, sg_policies)
        
        print(f"✅ Policy validation passed for security group '{args.get('name', 'unnamed')}'")
    
    def _validate_security_group_rule(self, args: Dict[str, Any]):
        """Validate adding a security group rule."""
        sg_policies = self.policies.get('security_groups', {})
        rule = args.get('rule', {})
        
        self._validate_single_security_group_rule(rule, sg_policies)
        
        print(f"✅ Policy validation passed for security group rule")
    
    def _validate_single_security_group_rule(self, rule: Dict[str, Any], sg_policies: Dict):
        """Validate a single security group rule."""
        rule_type = str(rule.get('type', 'ingress')).lower()
        
        # Only check ingress rules for now
        if rule_type != 'ingress':
            return

        protocol = str(rule.get('protocol', 'tcp')).lower()
        allowed_protocols = {
            str(p).lower() for p in sg_policies.get('allowed_protocols', ['tcp', 'udp', 'icmp', '-1'])
        }
        if protocol not in allowed_protocols:
            raise PolicyViolation(
                f"Protocol '{protocol}' is not allowed. "
                f"Allowed protocols: {', '.join(sorted(allowed_protocols))}"
            )

        cidr = str(rule.get('cidr', '') or '').strip()
        has_source_security_group = bool(rule.get('source_security_group_id'))

        # In the AWS security manager, missing source defaults to 0.0.0.0/0.
        if not cidr and not has_source_security_group:
            cidr = '0.0.0.0/0'

        if cidr and not self._is_valid_cidr(cidr):
            raise PolicyViolation(f"Invalid CIDR format '{cidr}' in security group rule")

        is_public_source = cidr in {'0.0.0.0/0', '::/0'}

        from_port, to_port = self._extract_port_range(rule, protocol)
        
        # Check for blocked ports
        blocked_ports = {int(p) for p in sg_policies.get('blocked_inbound_ports', [])}
        if from_port is not None and to_port is not None and self._range_overlaps_ports(from_port, to_port, blocked_ports):
            blocked_cidrs = sg_policies.get('blocked_cidrs_for_sensitive_ports', ['0.0.0.0/0'])
            if self._cidr_matches_any(cidr, blocked_cidrs):
                raise PolicyViolation(
                    f"SECURITY VIOLATION: Port range {from_port}-{to_port} cannot be opened to {cidr}. "
                    f"This would expose sensitive services to the public internet. "
                    f"Use a bastion host or VPN instead."
                )
        
        # Check for public access: only explicitly allowed ports may be open to the internet.
        enforce_public_allowlist = bool(sg_policies.get('enforce_public_ingress_port_allowlist', True))
        allowed_public_ports = {int(p) for p in sg_policies.get('allowed_public_ports', [])}
        if enforce_public_allowlist and is_public_source:
            if protocol == '-1':
                raise PolicyViolation(
                    "SECURITY VIOLATION: Opening all protocols/ports to the public is forbidden."
                )
            if protocol not in {'tcp', 'udp'}:
                raise PolicyViolation(
                    f"SECURITY VIOLATION: Public ingress for protocol '{protocol}' is not allowed by policy."
                )
            if from_port is None or to_port is None:
                raise PolicyViolation(
                    "SECURITY VIOLATION: Public TCP/UDP rules must include an explicit port or range."
                )
            disallowed_ports = [str(port) for port in range(from_port, to_port + 1) if port not in allowed_public_ports]
            if disallowed_ports:
                raise PolicyViolation(
                    f"SECURITY VIOLATION: Public ingress port range {from_port}-{to_port} is not allowed for {cidr}. "
                    f"Only ports {sorted(allowed_public_ports)} may be public."
                )

    def _validate_ssm_run_command(self, args: Dict[str, Any]):
        """Validate SSM Run Command usage to prevent destructive operations."""
        ssm_policies = self.policies.get('ssm', {})
        if not ssm_policies.get('enabled', True):
            raise PolicyViolation("SSM command execution is disabled by policy")

        commands = args.get('commands', [])
        if not isinstance(commands, list) or not commands:
            raise PolicyViolation("SSM run_command requires a non-empty commands list")

        max_commands = int(ssm_policies.get('max_commands_per_invocation', 10))
        if len(commands) > max_commands:
            raise PolicyViolation(
                f"Too many commands in one SSM invocation ({len(commands)}). "
                f"Maximum allowed is {max_commands}."
            )

        max_timeout = int(ssm_policies.get('max_timeout_seconds', 900))
        timeout_seconds = int(args.get('timeout_seconds', 600))
        if timeout_seconds > max_timeout:
            raise PolicyViolation(
                f"SSM timeout {timeout_seconds}s exceeds policy maximum of {max_timeout}s"
            )

        target_os_provided = bool(args.get('target_os'))
        target_os = self._resolve_target_os(args, ssm_policies)
        self._validate_target_os(target_os, ssm_policies)

        max_command_length = int(ssm_policies.get('max_command_length', 300))
        for command in commands:
            if not isinstance(command, str) or not command.strip():
                raise PolicyViolation("Each SSM command must be a non-empty string")
            normalized_command = command.strip()
            if len(normalized_command) > max_command_length:
                raise PolicyViolation(
                    f"Command length exceeds policy maximum ({max_command_length} characters): {normalized_command[:80]}..."
                )
            self._validate_ssm_single_command(
                command=normalized_command,
                target_os=target_os,
                target_os_provided=target_os_provided,
                ssm_policies=ssm_policies,
            )

        print(
            f"✅ Policy validation passed for SSM command execution "
            f"({len(commands)} command(s), target_os={target_os})"
        )

    def _validate_ssm_collect_host_diagnostics(self, args: Dict[str, Any]):
        """Validate generic host diagnostics collection tool arguments."""
        ssm_policies = self.policies.get('ssm', {})
        if not ssm_policies.get('enabled', True):
            raise PolicyViolation("SSM diagnostics execution is disabled by policy")

        target_os = self._resolve_target_os(args, ssm_policies)
        self._validate_target_os(target_os, ssm_policies)

        max_timeout = int(ssm_policies.get('max_timeout_seconds', 900))
        completion_timeout_seconds = int(args.get('completion_timeout_seconds', 120))
        if completion_timeout_seconds > max_timeout:
            raise PolicyViolation(
                f"Diagnostics timeout {completion_timeout_seconds}s exceeds policy maximum of {max_timeout}s"
            )

        print(f"✅ Policy validation passed for SSM host diagnostics (target_os={target_os})")

    def _validate_ssm_safe_disk_cleanup(self, args: Dict[str, Any]):
        """Validate bounded disk cleanup tool arguments."""
        ssm_policies = self.policies.get('ssm', {})
        if not ssm_policies.get('enabled', True):
            raise PolicyViolation("SSM cleanup execution is disabled by policy")

        target_os = self._resolve_target_os(args, ssm_policies)
        self._validate_target_os(target_os, ssm_policies)

        max_timeout = int(ssm_policies.get('max_timeout_seconds', 900))
        completion_timeout_seconds = int(args.get('completion_timeout_seconds', 180))
        if completion_timeout_seconds > max_timeout:
            raise PolicyViolation(
                f"Disk cleanup timeout {completion_timeout_seconds}s exceeds policy maximum of {max_timeout}s"
            )

        journal_vacuum_days = int(args.get('journal_vacuum_days', 7))
        if journal_vacuum_days < 1 or journal_vacuum_days > 30:
            raise PolicyViolation("journal_vacuum_days must be between 1 and 30")

        print(
            "✅ Policy validation passed for SSM safe disk cleanup "
            f"(target_os={target_os}, journal_vacuum_days={journal_vacuum_days})"
        )

    def _validate_ssm_service_operation(self, tool_name: str, args: Dict[str, Any]):
        """Validate SSM systemd service operations."""
        ssm_policies = self.policies.get('ssm', {})
        target_os = self._resolve_target_os(args, ssm_policies)
        self._validate_target_os(target_os, ssm_policies)

        supported_oses = {
            str(os_name).strip().lower()
            for os_name in ssm_policies.get(
                'systemd_supported_oses',
                ['amazon-linux-2023', 'amazon-linux', 'rhel', 'centos', 'rocky', 'almalinux', 'ubuntu', 'debian', 'suse'],
            )
            if str(os_name).strip()
        }
        if target_os not in supported_oses:
            raise PolicyViolation(
                f"Tool {tool_name} uses systemctl but target_os '{target_os}' is not in systemd-supported OS list"
            )

        if tool_name == 'aws_ssm_list_running_services':
            print(f"✅ Policy validation passed for SSM service listing (target_os={target_os})")
            return

        service_name = str(args.get('service_name', '') or '').strip()
        if not service_name:
            raise PolicyViolation(f"Tool {tool_name} requires a non-empty service_name")
        if not self.SERVICE_NAME_PATTERN.match(service_name):
            raise PolicyViolation(
                f"Invalid service_name '{service_name}'. Only systemd unit-safe characters are allowed."
            )

        normalized_service_name = service_name[:-8] if service_name.endswith('.service') else service_name
        blocked_services = {
            str(service).strip().lower()
            for service in ssm_policies.get('blocked_service_names', list(self.DEFAULT_BLOCKED_SSM_SERVICES))
            if str(service).strip()
        }
        if normalized_service_name.lower() in blocked_services or service_name.lower() in blocked_services:
            raise PolicyViolation(
                f"Service operation blocked by policy for critical service '{service_name}'"
            )

        print(
            f"✅ Policy validation passed for SSM service operation '{tool_name}' "
            f"(service={service_name}, target_os={target_os})"
        )

    def _validate_ssm_single_command(
        self,
        command: str,
        target_os: str,
        target_os_provided: bool,
        ssm_policies: Dict[str, Any],
    ):
        """Validate one shell command string for high-risk operations."""
        blocked_patterns = ssm_policies.get('blocked_command_patterns', self.DEFAULT_BLOCKED_SSM_COMMAND_PATTERNS)
        for pattern in blocked_patterns:
            try:
                if re.search(pattern, command, flags=re.IGNORECASE):
                    raise PolicyViolation(
                        f"SSM command blocked by security policy. Matched forbidden pattern '{pattern}'."
                    )
            except re.error:
                if str(pattern).lower() in command.lower():
                    raise PolicyViolation(
                        f"SSM command blocked by security policy. Matched forbidden token '{pattern}'."
                    )

        package_managers_used = self._detect_package_managers(command)
        if package_managers_used:
            if ssm_policies.get('require_target_os_for_package_changes', False) and not target_os_provided:
                raise PolicyViolation(
                    "Package-management commands require explicit target_os (for example: amazon-linux-2023, ubuntu)."
                )

            allowed_by_os = ssm_policies.get('allowed_package_managers_by_os', {})
            allowed_managers_for_os = {
                str(manager).strip().lower()
                for manager in allowed_by_os.get(target_os, [])
                if str(manager).strip()
            }
            if allowed_managers_for_os:
                disallowed = sorted(package_managers_used - allowed_managers_for_os)
                if disallowed:
                    raise PolicyViolation(
                        f"Command uses package manager(s) {disallowed} not allowed for target_os '{target_os}'. "
                        f"Allowed: {sorted(allowed_managers_for_os)}"
                    )

    def _resolve_target_os(self, args: Dict[str, Any], ssm_policies: Dict[str, Any]) -> str:
        """Resolve target OS for SSM policy checks."""
        target_os = args.get('target_os') or ssm_policies.get('default_target_os', 'amazon-linux-2023')
        return str(target_os).strip().lower()

    def _validate_target_os(self, target_os: str, ssm_policies: Dict[str, Any]):
        """Ensure target OS is in the allowed set when configured."""
        allowed_target_oses = {
            str(os_name).strip().lower()
            for os_name in ssm_policies.get('allowed_target_oses', [])
            if str(os_name).strip()
        }
        if allowed_target_oses and target_os not in allowed_target_oses:
            raise PolicyViolation(
                f"target_os '{target_os}' is not allowed. "
                f"Allowed target_os values: {sorted(allowed_target_oses)}"
            )

    def _detect_package_managers(self, command: str) -> Set[str]:
        """Return package manager commands referenced in a shell command."""
        matches = re.findall(r"\b(apt-get|apt|yum|dnf|zypper|pacman|rpm)\b", command.lower())
        return set(matches)

    def _extract_port_range(self, rule: Dict[str, Any], protocol: str) -> Tuple[Optional[int], Optional[int]]:
        """Extract and validate rule port range for TCP/UDP ingress checks."""
        if protocol not in {'tcp', 'udp'}:
            if protocol == '-1':
                return (0, 65535)
            return (None, None)

        if 'from_port' in rule or 'to_port' in rule:
            from_port = self._coerce_port(rule.get('from_port'))
            to_port = self._coerce_port(rule.get('to_port', from_port))
        elif 'port' in rule:
            from_port = self._coerce_port(rule.get('port'))
            to_port = from_port
        else:
            raise PolicyViolation("TCP/UDP ingress rule must include 'port' or 'from_port'/'to_port'")

        if from_port > to_port:
            raise PolicyViolation(
                f"Invalid port range: from_port {from_port} cannot be greater than to_port {to_port}"
            )

        return (from_port, to_port)

    def _coerce_port(self, value: Any) -> int:
        """Convert a port value to int with range validation."""
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise PolicyViolation(f"Invalid port value '{value}'")

        if port < 0 or port > 65535:
            raise PolicyViolation(f"Port value {port} is out of allowed range 0-65535")
        return port

    def _range_overlaps_ports(self, from_port: int, to_port: int, ports: Set[int]) -> bool:
        """Return True when any blocked port is inside [from_port, to_port]."""
        return any(from_port <= port <= to_port for port in ports)

    def _cidr_matches_any(self, cidr: str, blocked_cidrs: List[str]) -> bool:
        """Match CIDR against blocked CIDRs with normalized network equality."""
        candidate = self._normalize_cidr(cidr)
        blocked_normalized = {self._normalize_cidr(item) for item in blocked_cidrs}
        return candidate in blocked_normalized

    def _normalize_cidr(self, cidr: str) -> str:
        """Normalize CIDR string for policy matching."""
        if not cidr:
            return ''
        try:
            return str(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            return cidr.strip()

    def _is_valid_cidr(self, cidr: str) -> bool:
        """Return True when cidr is syntactically valid."""
        try:
            ipaddress.ip_network(cidr, strict=False)
            return True
        except ValueError:
            return False
    
    def _validate_nat_gateway_creation(self, args: Dict[str, Any]):
        """Validate NAT Gateway creation."""
        nat_policies = self.policies.get('nat_gateway', {})

        if nat_policies.get('require_justification', False):
            justification = args.get('justification')
            if not isinstance(justification, str) or not justification.strip():
                self._emit_cost_violation_or_recommendation(
                    "NAT gateway creation requires a non-empty justification.",
                    code='nat_missing_justification',
                )

        print(f"⚠️  NAT Gateway will incur costs (~$0.045/hour + data transfer)")
        print(f"✅ Policy validation passed for NAT Gateway '{args.get('name', 'unnamed')}'")

    def _validate_ec2_resize(self, args: Dict[str, Any]):
        """Validate EC2 resize operation safety and governance."""
        cost_cfg = self.policies.get('cost_optimization', {})

        create_backup = bool(args.get('create_backup', True))
        if cost_cfg.get('require_backup_before_resize', True) and not create_backup:
            self._emit_cost_violation_or_recommendation(
                "Resize requires AMI backup before instance type change.",
                code='resize_requires_backup',
            )

        target_type = args.get('target_instance_type')
        if not target_type:
            # Attribute-based selection path is allowed when target is omitted.
            return

        target_hourly = get_estimated_hourly_cost(str(target_type))
        max_resize_hourly = float(cost_cfg.get('max_resize_target_hourly_cost', 9999.0))
        if target_hourly > max_resize_hourly:
            self._emit_cost_violation_or_recommendation(
                (
                    f"Requested resize target '{target_type}' estimated at ${target_hourly:.4f}/hr "
                    f"exceeds max_resize_target_hourly_cost ${max_resize_hourly:.4f}/hr"
                ),
                code='resize_target_too_expensive',
            )

    def _validate_alarm_creation(self, args: Dict[str, Any]):
        """Flag potentially stale/high-noise alarm setup for small environments."""
        cost_cfg = self.policies.get('cost_optimization', {})
        if not cost_cfg.get('limit_stale_alarm_sprawl', True):
            return

        evaluation_periods = int(args.get('evaluation_periods', 1))
        period = int(args.get('period', 60))
        if evaluation_periods * period < 300:
            self.pending_recommendations.append(
                {
                    'domain': 'cost_optimization',
                    'code': 'alarm_too_short_window',
                    'message': (
                        "Alarm evaluation window is very short (<5 min). "
                        "This may increase noisy/stale alarms and operational cost."
                    ),
                    'mode': 'recommend',
                }
            )
    
    def estimate_cost(self, tool_name: str, arguments: Dict[str, Any]) -> float:
        """
        Estimate hourly cost of creating a resource.
        
        Args:
            tool_name: Name of the tool
            arguments: Tool arguments
        
        Returns:
            Estimated hourly cost in USD
        """
        # Simple cost estimation (us-east-1 pricing)
        if tool_name == "aws_create_ec2_instance":
            cpu = arguments.get('cpu', 2)
            ram = arguments.get('ram', 4)
            
            # Simple mapping (approximate)
            if cpu == 1 and ram == 1:
                return 0.0104  # t3.micro
            elif cpu == 2 and ram <= 4:
                return 0.0416  # t3.medium
            elif cpu == 2 and ram <= 8:
                return 0.0832  # t3.large
            else:
                return 0.1664  # t3.xlarge (estimate)
        
        elif tool_name == "aws_create_nat_gateway":
            return 0.045  # NAT Gateway hourly cost
        
        return 0.0  # Unknown or free resource
