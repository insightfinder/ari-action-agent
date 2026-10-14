---
name: tls-certificate-check
description: Checks the TLS certificates of HTTPS endpoints and reports whether each one is valid, who issued it, and how many days remain before it expires. Use when someone asks whether a site's certificate is valid or expired, when it expires, or why browsers or clients reject a host's certificate.
compatibility: Needs python3 (standard library only) and outbound network access to the endpoints being checked.
metadata:
  author: insightfinder
  version: "1.1"
---

# TLS certificate check

Use the bundled script. It connects to each endpoint, validates the certificate chain and host name with the system trust store, and prints one JSON document.

## Run it

Pass every host from the request in one call. A target can be a bare host, `host:port`, or an `https://` URL:

```
python3 <this skill's directory>/scripts/check_tls.py example.com api.example.com:8443
```

Options:

- `--warn-days N` marks a certificate as `expiring` when fewer than N days remain. The default is 21. Use the number the request gives, if any.
- `--port N` sets the port for targets that do not name one. The default is 443.
- `--deadline N` stops starting new checks after N seconds. The default is 90. Leave it alone unless the request names many hosts.

Use the absolute path of this skill's directory. Do not change into the directory first, do not use `$` variables, and do not pipe or redirect the output: those commands are refused.

## Read the result

Each entry in `results` has a `status`:

- `ok`: the certificate is valid and is not close to expiring.
- `expiring`: the certificate is valid, but `days_left` is below the warning threshold.
- `invalid`: the TLS handshake rejected the certificate. `error` says why, for example "certificate has expired", "self-signed certificate" or a host name mismatch. When the certificate could still be read, the entry also has `not_after`, `days_left` and `issuer`.
- `unreachable`: no TLS connection could be made. `error` gives the network error.
- `bad_target`: the target text could not be read as a host and port.
- `skipped`: the deadline passed before this target was checked.

The script exits with status 1 when any target is not `ok`. That is expected and is not a failure of the check itself.

## Report

For each target, give its status and, when present, the expiry date (`not_after`), the days left and the issuer. Put targets that need attention first. Quote `error` verbatim for `invalid`, `unreachable` and `skipped` targets. Do not guess at a fix the output does not support.
