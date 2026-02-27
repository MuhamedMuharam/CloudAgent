"""
AWS Resource Mapping
Maps generic compute specifications (CPU, RAM) to AWS instance types.
"""

# Instance type mapping based on CPU cores and RAM (GB)
# Format: (min_cpu, max_cpu, min_ram, max_ram): instance_type
INSTANCE_TYPE_MAPPING = [
    # t3 family - burstable, cost-effective
    (1, 1, 0, 2, 't3.micro'),      # 1 vCPU, 1 GB RAM
    (1, 1, 2, 4, 't3.small'),      # 1 vCPU, 2 GB RAM
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
    
    Note: These are approximate US East (N. Virginia) on-demand prices as of 2024.
    For production use, integrate with AWS Pricing API.
    
    Args:
        instance_type: AWS instance type (e.g., 't3.medium')
        region: AWS region (default: 'us-east-1')
    
    Returns:
        Estimated hourly cost in USD
    """
    # Approximate pricing (USD/hour) for us-east-1
    pricing = {
        't3.micro': 0.0104,
        't3.small': 0.0208,
        't3.medium': 0.0416,
        't3.large': 0.0832,
        't3.xlarge': 0.1664,
        't3.2xlarge': 0.3328,
        'm5.large': 0.096,
        'm5.xlarge': 0.192,
        'm5.2xlarge': 0.384,
        'c5.large': 0.085,
        'c5.xlarge': 0.17,
        'r5.large': 0.126,
        'r5.xlarge': 0.252,
    }
    
    return pricing.get(instance_type, 0.05)  # Default fallback
