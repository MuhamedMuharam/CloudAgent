"""
AWS Resource Mapping
Maps generic compute specifications (CPU, RAM) to AWS instance types.
"""

from typing import Dict, List, Optional, Tuple

# Instance type mapping based on CPU cores and RAM (GB)
# Format: (min_cpu, max_cpu, min_ram, max_ram): instance_type
INSTANCE_TYPE_MAPPING = [
    # t3 family - burstable, cost-effective
    (1, 1, 0, 1, 't3.nano'),       # Smallest burstable target
    (1, 1, 1, 2, 't3.micro'),      # 1 GB class workloads
    (1, 1, 2, 4, 't3.small'),      # 2 GB class workloads
    (2, 2, 0, 4, 't3.medium'),     # 2 vCPU, 4 GB RAM
    (2, 2, 4, 8, 't3.large'),      # 2 vCPU, 8 GB RAM
    (4, 4, 0, 16, 't3.xlarge'),    # 4 vCPU, 16 GB RAM
    (8, 8, 0, 32, 't3.2xlarge'),   # 8 vCPU, 32 GB RAM
    
    # m5 family - general purpose, balanced
    (2, 2, 8, 16, 'm5.large'),     # 2 vCPU, 8 GB RAM
    (4, 4, 16, 32, 'm5.xlarge'),   # 4 vCPU, 16 GB RAM
    (8, 8, 32, 64, 'm5.2xlarge'),  # 8 vCPU, 32 GB RAM
    
    # c5 family - compute-optimized
    (2, 2, 3.5, 5, 'c5.large'),    # 2 vCPU, 4 GB RAM (compute heavy)
    (4, 4, 7, 9, 'c5.xlarge'),     # 4 vCPU, 8 GB RAM
    
    # r5 family - memory-optimized
    (2, 2, 16, 24, 'r5.large'),    # 2 vCPU, 16 GB RAM (memory heavy)
    (4, 4, 32, 48, 'r5.xlarge'),   # 4 vCPU, 32 GB RAM
]


INSTANCE_TYPE_SPECS: Dict[str, Dict[str, float]] = {
    # t2
    't2.nano': {'vcpu': 1, 'ram_gb': 0.5},
    't2.micro': {'vcpu': 1, 'ram_gb': 1},
    't2.small': {'vcpu': 1, 'ram_gb': 2},
    't2.medium': {'vcpu': 2, 'ram_gb': 4},
    't2.large': {'vcpu': 2, 'ram_gb': 8},
    't2.xlarge': {'vcpu': 4, 'ram_gb': 16},
    't2.2xlarge': {'vcpu': 8, 'ram_gb': 32},
    # t3
    't3.nano': {'vcpu': 2, 'ram_gb': 0.5},
    't3.micro': {'vcpu': 2, 'ram_gb': 1},
    't3.small': {'vcpu': 2, 'ram_gb': 2},
    't3.medium': {'vcpu': 2, 'ram_gb': 4},
    't3.large': {'vcpu': 2, 'ram_gb': 8},
    't3.xlarge': {'vcpu': 4, 'ram_gb': 16},
    't3.2xlarge': {'vcpu': 8, 'ram_gb': 32},
    # t3a
    't3a.nano': {'vcpu': 2, 'ram_gb': 0.5},
    't3a.micro': {'vcpu': 2, 'ram_gb': 1},
    't3a.small': {'vcpu': 2, 'ram_gb': 2},
    't3a.medium': {'vcpu': 2, 'ram_gb': 4},
    't3a.large': {'vcpu': 2, 'ram_gb': 8},
    't3a.xlarge': {'vcpu': 4, 'ram_gb': 16},
    't3a.2xlarge': {'vcpu': 8, 'ram_gb': 32},
    # m5
    'm5.large': {'vcpu': 2, 'ram_gb': 8},
    'm5.xlarge': {'vcpu': 4, 'ram_gb': 16},
    'm5.2xlarge': {'vcpu': 8, 'ram_gb': 32},
    # c5
    'c5.large': {'vcpu': 2, 'ram_gb': 4},
    'c5.xlarge': {'vcpu': 4, 'ram_gb': 8},
    # r5
    'r5.large': {'vcpu': 2, 'ram_gb': 16},
    'r5.xlarge': {'vcpu': 4, 'ram_gb': 32},
}


INSTANCE_TYPE_PRICING_US_EAST_1 = {
    # Approximate Linux/Amazon Linux On-Demand rates in us-east-1.
    't2.nano': 0.0058,
    't2.micro': 0.0116,
    't2.small': 0.0230,
    't2.medium': 0.0464,
    't2.large': 0.0928,
    't2.xlarge': 0.1856,
    't2.2xlarge': 0.3712,
    't3.nano': 0.0052,
    't3.micro': 0.0104,
    't3.small': 0.0208,
    't3.medium': 0.0416,
    't3.large': 0.0832,
    't3.xlarge': 0.1664,
    't3.2xlarge': 0.3328,
    't3a.nano': 0.0047,
    't3a.micro': 0.0094,
    't3a.small': 0.0188,
    't3a.medium': 0.0376,
    't3a.large': 0.0752,
    't3a.xlarge': 0.1504,
    't3a.2xlarge': 0.3008,
    'm5.large': 0.096,
    'm5.xlarge': 0.192,
    'm5.2xlarge': 0.384,
    'c5.large': 0.085,
    'c5.xlarge': 0.17,
    'r5.large': 0.126,
    'r5.xlarge': 0.252,
}

def map_generic_to_instance_type(cpu: int, ram: int) -> str:
    """
    Map generic CPU and RAM requirements to an AWS EC2 instance type.
    
    Args:
        cpu: Number of virtual CPU cores requested
        ram: RAM in gigabytes (GB)
    
    Returns:
        AWS EC2 instance type string (e.g., 't3.medium')
    
    Raises:
        ValueError: If no suitable instance type found
    
    Examples:
        >>> map_generic_to_instance_type(2, 4)
        't3.medium'
        >>> map_generic_to_instance_type(4, 16)
        't3.xlarge'
    """
    # Find best matching instance type
    for min_cpu, max_cpu, min_ram, max_ram, instance_type in INSTANCE_TYPE_MAPPING:
        if min_cpu <= cpu <= max_cpu and min_ram <= ram <= max_ram:
            return instance_type
    
    # If no exact match, find the smallest instance that meets or exceeds requirements
    for min_cpu, max_cpu, min_ram, max_ram, instance_type in INSTANCE_TYPE_MAPPING:
        if cpu <= max_cpu and ram <= max_ram:
            return instance_type
    
    # If still no match, return a reasonable default with a warning
    print(f"Warning: No exact match for {cpu} CPUs and {ram}GB RAM. Defaulting to t3.medium")
    return 't3.medium'


def get_estimated_hourly_cost(instance_type: str, region: str = 'us-east-1') -> float:
    """
    Get estimated hourly cost for an instance type.
    
    Note: These are approximate US East (N. Virginia) Linux/Amazon Linux
    on-demand prices.
    For production use, integrate with AWS Pricing API.
    
    Args:
        instance_type: AWS instance type (e.g., 't3.medium')
        region: AWS region (default: 'us-east-1')
    
    Returns:
        Estimated hourly cost in USD
    """
    # Approximate pricing (USD/hour) for us-east-1
    return INSTANCE_TYPE_PRICING_US_EAST_1.get(instance_type, 0.05)  # Default fallback


def get_next_smaller_instance_type(
    current_instance_type: str,
    allowed_families: Optional[Tuple[str, ...]] = None,
) -> Optional[str]:
    """
    Return the next smaller size in the same family when available.

    Example: t3.small -> t3.micro
    """
    if not current_instance_type or '.' not in current_instance_type:
        return None

    family, size = current_instance_type.split('.', 1)
    if allowed_families and family not in allowed_families:
        return None

    size_order = [
        'nano', 'micro', 'small', 'medium', 'large',
        'xlarge', '2xlarge', '3xlarge', '4xlarge', '6xlarge',
        '8xlarge', '9xlarge', '10xlarge', '12xlarge', '16xlarge',
        '18xlarge', '24xlarge', '32xlarge',
    ]
    if size not in size_order:
        return None

    current_index = size_order.index(size)
    if current_index == 0:
        return None

    for idx in range(current_index - 1, -1, -1):
        candidate = f"{family}.{size_order[idx]}"
        if candidate in INSTANCE_TYPE_SPECS and candidate in INSTANCE_TYPE_PRICING_US_EAST_1:
            return candidate

    return None


def get_instance_catalog() -> List[Dict[str, float]]:
    """Return known instance catalog with specs and estimated hourly pricing."""
    catalog: List[Dict[str, float]] = []
    for instance_type, specs in INSTANCE_TYPE_SPECS.items():
        catalog.append(
            {
                'instance_type': instance_type,
                'vcpu': specs['vcpu'],
                'ram_gb': specs['ram_gb'],
                'hourly_cost': get_estimated_hourly_cost(instance_type),
            }
        )
    return catalog


def get_instance_type_specs(instance_type: str) -> Optional[Dict[str, float]]:
    """Return vCPU and RAM specs for a known instance type."""
    return INSTANCE_TYPE_SPECS.get(instance_type)


def find_cheapest_instance_type_for_requirements(
    cpu: int,
    ram: int,
    allowed_families: Optional[Tuple[str, ...]] = None,
) -> str:
    """
    Return the cheapest known instance type that satisfies CPU/RAM requirements.

    Args:
        cpu: Minimum required vCPUs
        ram: Minimum required RAM in GB
        allowed_families: Optional tuple of allowed families (e.g., ('t3', 'm5'))
    """
    candidates = []
    for instance_type, specs in INSTANCE_TYPE_SPECS.items():
        if specs['vcpu'] < cpu or specs['ram_gb'] < ram:
            continue

        family = instance_type.split('.')[0]
        if allowed_families and family not in allowed_families:
            continue

        hourly_cost = get_estimated_hourly_cost(instance_type)
        candidates.append((hourly_cost, specs['vcpu'], specs['ram_gb'], instance_type))

    if not candidates:
        # Fall back to the legacy mapper behavior when no direct candidate is found.
        return map_generic_to_instance_type(cpu, ram)

    # Sort by cost first, then by smallest sufficient shape.
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]
