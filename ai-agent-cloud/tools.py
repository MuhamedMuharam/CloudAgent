# tools.py

cloud_state = {
    "vms": []
}

def list_vms():
    return cloud_state["vms"]

def create_vm(name, cpu=1, ram=1):
    vm = {
        "name": name,
        "cpu": cpu,
        "ram": ram,
        "status": "running"
    }
    cloud_state["vms"].append(vm)
    return f"VM {name} created successfully."

def delete_vm(name):
    for vm in cloud_state["vms"]:
        if vm["name"] == name:
            cloud_state["vms"].remove(vm)
            return f"VM {name} deleted."
    return "VM not found."