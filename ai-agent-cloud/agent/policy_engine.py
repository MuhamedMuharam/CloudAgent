"""
Policy Engine
Validates AI agent actions against security and compliance policies.

This is a critical safety component that prevents the agent from:
- Creating expensive resources
- Opening dangerous security group rules
- Violating organizational policies
"""

import yaml
from pathlib import Path
from typing import Dict, Any, List


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
        
        if self.policies:
            print(f"✅ Policy engine loaded with {len(self.policies)} policy sections")
        else:
            print("⚠️  No policies loaded - running without policy enforcement")
    
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
        elif tool_name == "aws_create_nat_gateway":
            self._validate_nat_gateway_creation(arguments)
        # Add more validators as needed
    
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
        rule_type = rule.get('type', 'ingress')
        
        # Only check ingress rules for now
        if rule_type != 'ingress':
            return
        
        # Get port
        port = rule.get('port') or rule.get('from_port', 0)
        cidr = rule.get('cidr', '')
        
        # Check for blocked ports
        blocked_ports = sg_policies.get('blocked_inbound_ports', [])
        if port in blocked_ports:
            # Check if it's from a restricted CIDR
            blocked_cidrs = sg_policies.get('blocked_cidrs_for_sensitive_ports', [])
            if cidr in blocked_cidrs:
                raise PolicyViolation(
                    f"SECURITY VIOLATION: Port {port} cannot be opened to {cidr}. "
                    f"This would expose sensitive services to the public internet. "
                    f"Use a bastion host or VPN instead."
                )
        
        # Check for public access to non-public ports
        allowed_public_ports = sg_policies.get('allowed_public_ports', [])
        if cidr == '0.0.0.0/0' and port not in allowed_public_ports:
            if port in blocked_ports:
                raise PolicyViolation(
                    f"SECURITY VIOLATION: Port {port} cannot be opened to the public (0.0.0.0/0). "
                    f"Only ports {allowed_public_ports} can be publicly accessible."
                )
        
        # Check protocol
        protocol = rule.get('protocol', 'tcp')
        allowed_protocols = sg_policies.get('allowed_protocols', ['tcp', 'udp', 'icmp', '-1'])
        if protocol not in allowed_protocols:
            raise PolicyViolation(
                f"Protocol '{protocol}' is not allowed. "
                f"Allowed protocols: {', '.join(allowed_protocols)}"
            )
    
    def _validate_nat_gateway_creation(self, args: Dict[str, Any]):
        """Validate NAT Gateway creation."""
        nat_policies = self.policies.get('nat_gateway', {})
        
        # NAT Gateways cost money - just warn for now
        print(f"⚠️  NAT Gateway will incur costs (~$0.045/hour + data transfer)")
        print(f"✅ Policy validation passed for NAT Gateway '{args.get('name', 'unnamed')}'")
    
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
