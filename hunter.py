"""Try once to create the Always Free A1 server; exit quietly on 'out of capacity'.

Run by .github/workflows/hunt.yml on a schedule. On success it opens a GitHub
issue (which emails the repo owner) and disables the workflow.
"""
import base64
import os
import sys
import time

import oci

NAME = "free-server"
SHAPE = "VM.Standard.A1.Flex"
OCPUS, MEMORY_GB, BOOT_GB = 2, 12, 200  # the whole Always Free A1 allowance
VCN_NAME = "free-server-vcn"
# Oracle's Ubuntu images block 80/443 in iptables; open them on first boot.
OPEN_WEB_PORTS = base64.b64encode(b"""#!/bin/bash
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
netfilter-persistent save
""").decode()


def is_capacity_error(e):
    msg = (getattr(e, "message", "") or "").lower()
    return getattr(e, "status", None) in (429, 500) or "capacity" in msg or "too many requests" in msg


def config():
    cfg = {
        "tenancy": os.environ["OCI_TENANCY_OCID"],
        "user": os.environ["OCI_USER_OCID"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "key_content": os.environ["OCI_PRIVATE_KEY"],
        "region": os.environ["OCI_REGION"],
    }
    oci.config.validate_config(cfg)
    return cfg


def a1_ocpus_in_use(compute, tenancy):
    used, existing = 0, None
    for i in oci.pagination.list_call_get_all_results(compute.list_instances, tenancy).data:
        if i.lifecycle_state in ("TERMINATED", "TERMINATING"):
            continue
        if i.display_name == NAME:
            existing = i
        if i.shape == SHAPE:
            used += i.shape_config.ocpus
    return used, existing


def ensure_network(net, tenancy):
    """Return a public subnet id, creating VCN + internet gateway + ports 22/80/443 if missing."""
    vcns = [v for v in net.list_vcns(tenancy).data if v.display_name == VCN_NAME and v.lifecycle_state == "AVAILABLE"]
    if vcns:
        vcn = vcns[0]
        subnets = net.list_subnets(tenancy, vcn_id=vcn.id).data
        if subnets:
            return subnets[0].id
    else:
        vcn = net.create_vcn(oci.core.models.CreateVcnDetails(
            compartment_id=tenancy, display_name=VCN_NAME, cidr_blocks=["10.0.0.0/16"], dns_label="freevcn")).data
        oci.wait_until(net, net.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE")

    igw = net.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
        compartment_id=tenancy, vcn_id=vcn.id, is_enabled=True, display_name="free-server-igw")).data
    oci.wait_until(net, net.get_internet_gateway(igw.id), "lifecycle_state", "AVAILABLE")
    net.update_route_table(vcn.default_route_table_id, oci.core.models.UpdateRouteTableDetails(route_rules=[
        oci.core.models.RouteRule(destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id)]))

    sl = net.get_security_list(vcn.default_security_list_id).data
    rules = list(sl.ingress_security_rules)
    for port in (80, 443):
        rules.append(oci.core.models.IngressSecurityRule(protocol="6", source="0.0.0.0/0",
            tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=port, max=port))))
    net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(ingress_security_rules=rules))

    subnet = net.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=tenancy, vcn_id=vcn.id, cidr_block="10.0.0.0/24", display_name="free-server-subnet",
        dns_label="public", prohibit_public_ip_on_vnic=False)).data
    oci.wait_until(net, net.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE")
    return subnet.id


def ubuntu_image(compute, tenancy):
    imgs = compute.list_images(tenancy, operating_system="Canonical Ubuntu", operating_system_version="24.04",
                               shape=SHAPE, sort_by="TIMECREATED", sort_order="DESC").data
    imgs = [i for i in imgs if "Minimal" not in i.display_name]
    if not imgs:
        raise RuntimeError("No Ubuntu 24.04 ARM image found")
    return imgs[0].id


def public_ip(compute, net, tenancy, instance_id):
    for att in compute.list_vnic_attachments(tenancy, instance_id=instance_id).data:
        ip = net.get_vnic(att.vnic_id).data.public_ip
        if ip:
            return ip
    return None


def write_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def main():
    cfg = config()
    tenancy = cfg["tenancy"]
    compute = oci.core.ComputeClient(cfg)
    net = oci.core.VirtualNetworkClient(cfg)
    ident = oci.identity.IdentityClient(cfg)

    used, existing = a1_ocpus_in_use(compute, tenancy)
    if existing:
        print(f"{NAME} already exists ({existing.lifecycle_state}); nothing to do.")
        write_output("created", "already")
        return
    if used + OCPUS > 2:
        print(f"{used} A1 OCPUs already in use; creating more would leave Always Free. Stopping.")
        write_output("created", "limit")
        return

    subnet_id = ensure_network(net, tenancy)
    image_id = ubuntu_image(compute, tenancy)
    ad = ident.list_availability_domains(tenancy).data[0].name

    try:
        inst = compute.launch_instance(oci.core.models.LaunchInstanceDetails(
            compartment_id=tenancy, availability_domain=ad, display_name=NAME, shape=SHAPE,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEMORY_GB),
            source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=BOOT_GB),
            create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subnet_id, assign_public_ip=True),
            metadata={"ssh_authorized_keys": os.environ["SSH_PUBLIC_KEY"], "user_data": OPEN_WEB_PORTS})).data
    except oci.exceptions.ServiceError as e:
        if is_capacity_error(e):
            print(f"Out of capacity ({e.status} {e.code}); will retry on the next run.")
            write_output("created", "no")
            return
        raise

    print(f"Launched {inst.id}; waiting for RUNNING...")
    oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING", max_wait_seconds=900)
    ip = None
    for _ in range(20):
        ip = public_ip(compute, net, tenancy, inst.id)
        if ip:
            break
        time.sleep(15)
    print(f"Created! Public IP: {ip}")
    write_output("created", "yes")
    write_output("ip", ip or "unknown")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        class E:  # minimal stand-in for oci ServiceError
            def __init__(self, status, message): self.status, self.message = status, message
        assert is_capacity_error(E(500, "Out of host capacity."))
        assert is_capacity_error(E(429, "Too many requests for the user"))
        assert not is_capacity_error(E(401, "The required information to complete authentication was not provided."))
        print("self-test ok")
    else:
        main()
