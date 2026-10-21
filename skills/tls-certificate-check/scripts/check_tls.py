import argparse
import ipaddress
import json
import os
import socket
import ssl
import sys
import tempfile
import time
from datetime import datetime, timezone


def parse_target(text, default_port):
    value = text.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0]
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        host, port = value.split(":")
    elif ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            raise ValueError(f"cannot read a host and port from {text!r}") from None
        host, port = value, ""
    else:
        host, port = value, ""
    host = host.rstrip(".")
    if not host:
        raise ValueError(f"no host in {text!r}")
    if port and not port.isdigit():
        raise ValueError(f"port must be a number in {text!r}")
    number = int(port) if port else default_port
    if not 0 < number < 65536:
        raise ValueError(f"port out of range in {text!r}")
    return host.lower(), number


def names(entries, key):
    for group in entries or ():
        for name, value in group:
            if name == key:
                return value
    return None


def describe_certificate(cert, now):
    not_before = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notBefore"]), tz=timezone.utc)
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
    return {
        "days_left": int((not_after - now).total_seconds() // 86400),
        "not_before": not_before.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "not_after": not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "subject": names(cert.get("subject"), "commonName"),
        "issuer": names(cert.get("issuer"), "organizationName") or names(cert.get("issuer"), "commonName"),
        "dns_names": [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"],
    }


def unverified_certificate(host, port, timeout):
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        return None
    handle, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(ssl.DER_cert_to_PEM_cert(der))
        return decode(path)
    finally:
        os.unlink(path)


def check(host, port, timeout, warn_days, now):
    result = {"target": f"[{host}]:{port}" if ":" in host else f"{host}:{port}"}
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
                result["tls_version"] = tls.version()
    except ssl.SSLCertVerificationError as exc:
        result.update(status="invalid", error=getattr(exc, "verify_message", None) or str(exc))
        try:
            details = unverified_certificate(host, port, timeout)
            if details:
                result.update(describe_certificate(details, now))
        except (OSError, ValueError, KeyError):
            pass
        return result
    except (OSError, ssl.SSLError) as exc:
        result.update(status="unreachable", error=str(exc) or exc.__class__.__name__)
        return result
    except ValueError as exc:
        result.update(status="bad_target", error=str(exc) or exc.__class__.__name__)
        return result
    details = describe_certificate(cert, now)
    result.update(status="expiring" if details["days_left"] < warn_days else "ok", **details)
    return result


def main(argv=None, clock=time.monotonic):
    parser = argparse.ArgumentParser(description="Report TLS certificate validity and expiry for one or more endpoints.")
    parser.add_argument("targets", nargs="+", help="host, host:port or https:// URL")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--warn-days", type=int, default=21)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--deadline", type=float, default=90.0, help="seconds for the whole run")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    started = clock()
    results = []
    for text in args.targets:
        if clock() - started > args.deadline:
            results.append({"target": text, "status": "skipped", "error": f"not checked: the {args.deadline:g}s deadline passed"})
            continue
        try:
            host, port = parse_target(text, args.port)
        except ValueError as exc:
            results.append({"target": text, "status": "bad_target", "error": str(exc)})
            continue
        results.append(check(host, port, args.timeout, args.warn_days, now))
    json.dump({"checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "warn_days": args.warn_days, "results": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if all(item["status"] == "ok" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
