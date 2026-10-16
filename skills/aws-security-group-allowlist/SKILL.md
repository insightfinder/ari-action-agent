---
name: aws-security-group-allowlist
description: Adds one person's public IP address to the operator-configured AWS security groups, for example so a teammate on a new VPN or home address can reach internal hosts. Use when someone asks to allow, whitelist or add an IP address to the security groups, often written as just an address and a label such as "104.245.103.27 vpn".
compatibility: Needs python3, boto3, and AWS credentials with ec2:DescribeSecurityGroups and ec2:AuthorizeSecurityGroupIngress. The operator must add a config.json next to this SKILL.md listing the security group ids it may change.
metadata:
  author: insightfinder
  version: "1.2"
---

# Allow an IP address into the security groups

This skill changes AWS firewall rules, so it always looks before it changes anything.

## 1. Read the request

You need:

- the address: exactly one public IPv4 or IPv6 address. Refuse ranges, private addresses, and requests for more than one address at a time;
- a label: who or what the address is for, for example "alice vpn". Use the words the request gives. If it gives none, leave the label out;
- optionally, specific security group ids, only if the request names them.

## 2. Dry run first

```
python3 <this skill's directory>/scripts/allow_ip.py <address> "<label>"
```

Add `--sg <id>` once per group only when the request names groups. The script refuses any group that is not in the operator's `config.json`. The dry run changes nothing. For every group it reports each rule as `already_present` or `would_add`.

## 3. Apply only when asked

Run the same command again with `--apply` only when the request clearly asks for the change to be made, for example "add", "allow" or "whitelist" this address. If the request only asks to check, or if you are unsure, stop after the dry run and report what would change.

If every rule is already `already_present`, do not run `--apply`.

## 4. Report

Give the address, the label, and for every security group its id, its name and what happened to each rule (`added`, `already_present`, `would_add`, or `error`). If the output has an `error`, quote it verbatim and do not retry with different arguments.

Use the absolute path of this skill's directory. Do not change into the directory first, do not use `$` variables, and do not pipe or redirect the output: those commands are refused.

## For the operator

Copy this folder into `SKILLS_DIR` and add `config.json` next to this file:

```json
{
  "groups": ["sg-0123456789abcdef0", "sg-0fedcba9876543210"],
  "permissions": "all"
}
```

`permissions` is `all` (every protocol and port) or a list such as `tcp:22,tcp:443,tcp:8000-8100`. The script reads this file from a fixed place, so nothing the agent sends can widen it. Without the file the script refuses to run.
