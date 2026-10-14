import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"
MAX_LABEL = 200
DESCRIPTION_UNSAFE = re.compile(r"[^a-zA-Z0-9 ._\-:/()#,@\[\]+=&;{}!$*]")


class UsageError(Exception):
    pass


def single_host(text):
    try:
        network = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        raise UsageError(f"{text!r} is not an IP address") from None
    if network.num_addresses != 1:
        raise UsageError(f"{text!r} covers {network.num_addresses} addresses; only a single address can be allowed")
    if not network.network_address.is_global:
        raise UsageError(f"{text!r} is not a public address")
    return network


def parse_permissions(text):
    permissions = []
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        if item == "all":
            permissions.append({"protocol": "-1", "from_port": None, "to_port": None})
            continue
        protocol, _, ports = item.partition(":")
        if protocol not in ("tcp", "udp") or not ports:
            raise UsageError(f"{item!r} is not 'all' or tcp:PORT, tcp:FROM-TO, udp:PORT")
        low, _, high = ports.partition("-")
        if not low.isdigit() or (high and not high.isdigit()):
            raise UsageError(f"{item!r} has a port that is not a number")
        start, end = int(low), int(high or low)
        if not 0 <= start <= end <= 65535:
            raise UsageError(f"{item!r} has a port range outside 0-65535")
        permissions.append({"protocol": protocol, "from_port": start, "to_port": end})
    if not permissions:
        raise UsageError("no permissions to add")
    return permissions


def load_config(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(f"{path} does not exist, so no security group may be changed") from None
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read {path}: {exc}") from None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not all(isinstance(g, str) and g.strip() for g in groups) or not groups:
        raise UsageError(f"{path} must list the allowed security group ids under \"groups\"")
    permissions = data.get("permissions", "all")
    if not isinstance(permissions, str):
        raise UsageError(f"\"permissions\" in {path} must be text such as \"all\" or \"tcp:22,tcp:443\"")
    return [g.strip() for g in groups], permissions


def allowed_groups(requested, configured):
    chosen = requested or configured
    outside = sorted(set(chosen) - set(configured))
    if outside:
        raise UsageError(f"not in the configured groups: {', '.join(outside)}")
    return list(dict.fromkeys(chosen))


def covers(existing, permission, network):
    if existing.get("IpProtocol") != permission["protocol"]:
        return False
    if permission["protocol"] != "-1":
        if existing.get("FromPort") != permission["from_port"] or existing.get("ToPort") != permission["to_port"]:
            return False
    key, field = ("Ipv6Ranges", "CidrIpv6") if network.version == 6 else ("IpRanges", "CidrIp")
    return any(entry.get(field) == str(network) for entry in existing.get(key, []))


def ip_permission(permission, network, description):
    item = {"IpProtocol": permission["protocol"]}
    if permission["protocol"] != "-1":
        item["FromPort"] = permission["from_port"]
        item["ToPort"] = permission["to_port"]
    if network.version == 6:
        item["Ipv6Ranges"] = [{"CidrIpv6": str(network), "Description": description}]
    else:
        item["IpRanges"] = [{"CidrIp": str(network), "Description": description}]
    return item


def describe(permission):
    if permission["protocol"] == "-1":
        return "all traffic"
    ports = str(permission["from_port"])
    if permission["to_port"] != permission["from_port"]:
        ports += f"-{permission['to_port']}"
    return f"{permission['protocol']} {ports}"


def error_text(exc):
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return f"{code}: {exc}" if code and code not in str(exc) else str(exc)


def run(client, groups, permissions, network, description, apply):
    report = []
    for group_id in groups:
        entry = {"group_id": group_id, "group_name": None, "rules": []}
        report.append(entry)
        try:
            found = client.describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
        except Exception as exc:
            entry["error"] = error_text(exc)
            continue
        group = found[0] if found else None
        if group is None:
            entry["error"] = "security group not found"
            continue
        entry["group_name"] = group.get("GroupName")
        for permission in permissions:
            rule = {"rule": describe(permission)}
            entry["rules"].append(rule)
            if any(covers(existing, permission, network) for existing in group.get("IpPermissions", [])):
                rule["state"] = "already_present"
                continue
            if not apply:
                rule["state"] = "would_add"
                continue
            try:
                client.authorize_security_group_ingress(
                    GroupId=group_id, IpPermissions=[ip_permission(permission, network, description)]
                )
                rule["state"] = "added"
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code == "InvalidPermission.Duplicate":
                    rule["state"] = "already_present"
                else:
                    rule.update(state="error", error=error_text(exc))
    return report


def make_client(region):
    try:
        import boto3
    except ImportError:
        raise UsageError("boto3 is not installed in this environment") from None
    return boto3.client("ec2", region_name=region) if region else boto3.client("ec2")


def main(argv=None, environ=None, client_factory=make_client, now=None, config_path=CONFIG_FILE):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Allow one public IP address into configured AWS security groups.")
    parser.add_argument("ip", help="the single public IPv4 or IPv6 address to allow")
    parser.add_argument("label", nargs="?", default="", help="who or what the address belongs to, e.g. 'alice vpn'")
    parser.add_argument("--sg", action="append", default=[], help="security group id; repeatable; must be configured")
    parser.add_argument("--region", default=environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION"))
    parser.add_argument("--apply", action="store_true", help="make the change; without it nothing is changed")
    args = parser.parse_args(argv)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    try:
        network = single_host(args.ip)
        configured, permission_text = load_config(config_path)
        groups = allowed_groups(args.sg, configured)
        permissions = parse_permissions(permission_text)
        label = " ".join(DESCRIPTION_UNSAFE.sub(" ", args.label).split())[:MAX_LABEL]
        description = f"{label} (added by ARI {stamp})" if label else f"added by ARI {stamp}"
        client = client_factory(args.region)
        groups_report = run(client, groups, permissions, network, description, args.apply)
    except UsageError as exc:
        json.dump({"error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 2
    except Exception as exc:
        json.dump({"error": f"{exc.__class__.__name__}: {exc}"}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    result = {
        "mode": "applied" if args.apply else "dry-run",
        "address": str(network),
        "description": description,
        "groups": groups_report,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    failed = any(g.get("error") or any(r.get("state") == "error" for r in g["rules"]) for g in groups_report)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
