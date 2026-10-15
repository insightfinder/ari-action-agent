import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common import skills  # noqa: E402

BUNDLED = ROOT / "skills"


def load_script(skill: str, script: str):
    path = BUNDLED / skill / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"skill_{skill.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_tls = load_script("tls-certificate-check", "check_tls.py")
allow_ip = load_script("aws-security-group-allowlist", "allow_ip.py")


def test_every_bundled_skill_follows_the_spec_without_warnings():
    reports = skills.scan(BUNDLED, "bundled")
    assert sorted(r.path.name for r in reports) == ["aws-security-group-allowlist", "changelog-entry", "tls-certificate-check"]
    for report in reports:
        assert report.ok and report.warnings == [], (report.path, report.errors, report.warnings)


def test_bundled_skills_directory_has_nothing_but_skills_and_its_readme():
    entries = sorted(p.name for p in BUNDLED.iterdir() if p.name != "__pycache__")
    assert entries == ["README.md", "aws-security-group-allowlist", "changelog-entry", "tls-certificate-check"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("example.com", ("example.com", 443)),
        ("Example.COM:8443", ("example.com", 8443)),
        ("example.com.", ("example.com", 443)),
        ("https://example.com./path?q=1", ("example.com", 443)),
        ("https://user:pw@example.com:9443/x", ("example.com", 9443)),
        ("[2001:db8::1]:8443", ("2001:db8::1", 8443)),
        ("2001:db8::1", ("2001:db8::1", 443)),
    ],
)
def test_tls_targets_are_read_from_hosts_ports_and_urls(text, expected):
    assert check_tls.parse_target(text, 443) == expected


@pytest.mark.parametrize("text", ["bad:port:x", "host:http", "host:70000", "https://", ":443", "."])
def test_unreadable_tls_targets_are_rejected(text):
    with pytest.raises(ValueError):
        check_tls.parse_target(text, 443)


def test_tls_report_marks_unreadable_targets_and_exits_nonzero(monkeypatch):
    monkeypatch.setattr(check_tls, "check", lambda host, port, timeout, warn, now: {"target": f"{host}:{port}", "status": "ok"})
    out = io.StringIO()
    with redirect_stdout(out):
        code = check_tls.main(["good.example", "bad:port:x"])
    report = json.loads(out.getvalue())
    assert code == 1
    assert [r["status"] for r in report["results"]] == ["ok", "bad_target"]
    out = io.StringIO()
    with redirect_stdout(out):
        assert check_tls.main(["good.example"]) == 0


def test_a_host_name_that_cannot_be_encoded_is_a_bad_target_not_a_traceback():
    result = check_tls.check("a" * 70 + ".example", 443, 1.0, 21, datetime.now(timezone.utc))
    assert result["status"] == "bad_target"


def test_targets_past_the_deadline_are_skipped_but_still_reported(monkeypatch):
    monkeypatch.setattr(check_tls, "check", lambda host, port, timeout, warn, now: {"target": host, "status": "ok"})
    ticks = iter([0.0, 1.0, 100.0, 101.0])
    out = io.StringIO()
    with redirect_stdout(out):
        code = check_tls.main(["a.example", "b.example", "c.example"], clock=lambda: next(ticks))
    statuses = [r["status"] for r in json.loads(out.getvalue())["results"]]
    assert statuses == ["ok", "skipped", "skipped"]
    assert code == 1


def test_an_invalid_certificate_still_reports_its_dates_when_they_can_be_read(monkeypatch):
    class Refused:
        def __init__(self, *a, **k):
            raise check_tls.ssl.SSLCertVerificationError(1, "certificate verify failed: certificate has expired")

    cert = {
        "notBefore": "Jan  1 00:00:00 2025 GMT",
        "notAfter": "Jan  1 00:00:00 2026 GMT",
        "subject": ((("commonName", "expired.example"),),),
        "issuer": ((("organizationName", "Example CA"),),),
        "subjectAltName": (("DNS", "expired.example"),),
    }
    monkeypatch.setattr(check_tls.socket, "create_connection", Refused)
    monkeypatch.setattr(check_tls, "unverified_certificate", lambda host, port, timeout: cert)
    result = check_tls.check("expired.example", 443, 1.0, 21, datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert result["status"] == "invalid"
    assert result["not_after"] == "2026-01-01T00:00:00Z"
    assert result["days_left"] < 0
    assert result["issuer"] == "Example CA"


class FakeEc2:
    def __init__(self, groups, fail_with=None):
        self.groups = groups
        self.authorized = []
        self.fail_with = fail_with

    def describe_security_groups(self, GroupIds):
        found = [g for g in self.groups if g["GroupId"] in GroupIds]
        if not found:
            raise NotFound(f"The security group '{GroupIds[0]}' does not exist")
        return {"SecurityGroups": found}

    def authorize_security_group_ingress(self, GroupId, IpPermissions):
        if self.fail_with:
            raise self.fail_with
        self.authorized.append((GroupId, IpPermissions))


class Duplicate(Exception):
    response = {"Error": {"Code": "InvalidPermission.Duplicate"}}


class NotFound(Exception):
    response = {"Error": {"Code": "InvalidGroup.NotFound"}}


CONFIG = {"groups": ["sg-a", "sg-b"]}
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture
def config_file(tmp_path):
    def write(data):
        path = tmp_path / "config.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    return write


def groups(*existing_cidrs):
    return [
        {"GroupId": "sg-a", "GroupName": "bastion", "IpPermissions": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": c} for c in existing_cidrs]}]},
        {"GroupId": "sg-b", "GroupName": "jenkins", "IpPermissions": []},
    ]


def run_main(args, client, config_path, environ=None):
    out = io.StringIO()
    with redirect_stdout(out):
        code = allow_ip.main(
            args, environ=environ or {}, client_factory=lambda region: client, now=NOW, config_path=config_path
        )
    return code, json.loads(out.getvalue())


def test_a_dry_run_changes_nothing_and_says_what_would_change(config_file):
    client = FakeEc2(groups("104.245.103.27/32"))
    code, report = run_main(["104.245.103.27", "alice  vpn"], client, config_file(CONFIG))
    assert code == 0 and client.authorized == []
    assert report["mode"] == "dry-run"
    assert report["description"] == "alice vpn (added by ARI 2026-10-14)"
    states = {g["group_id"]: [r["state"] for r in g["rules"]] for g in report["groups"]}
    assert states == {"sg-a": ["already_present"], "sg-b": ["would_add"]}


def test_the_label_is_cut_to_what_an_ec2_rule_description_accepts(config_file):
    client = FakeEc2(groups())
    code, report = run_main(["104.245.103.27", "Zoë's  <vpn>\t\"home\" " + "x" * 400], client, config_file(CONFIG))
    assert code == 0
    description = report["description"]
    assert description.startswith("Zo s vpn home xxx")
    assert len(description) <= 255
    assert not allow_ip.DESCRIPTION_UNSAFE.search(description)


def test_apply_adds_only_what_is_missing(config_file):
    client = FakeEc2(groups("104.245.103.27/32"))
    code, report = run_main(["104.245.103.27", "vpn", "--apply"], client, config_file(CONFIG))
    assert code == 0
    assert client.authorized == [
        ("sg-b", [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "104.245.103.27/32", "Description": "vpn (added by ARI 2026-10-14)"}]}])
    ]
    assert [r["state"] for r in report["groups"][1]["rules"]] == ["added"]


def test_a_duplicate_reported_by_aws_counts_as_present(config_file):
    client = FakeEc2(groups(), fail_with=Duplicate("dup"))
    code, report = run_main(["104.245.103.27", "--apply", "--sg", "sg-b"], client, config_file(CONFIG))
    assert code == 0
    assert [g["group_id"] for g in report["groups"]] == ["sg-b"]
    assert report["groups"][0]["rules"][0]["state"] == "already_present"


def test_other_aws_errors_are_reported_and_fail_the_run(config_file):
    client = FakeEc2(groups(), fail_with=RuntimeError("AccessDenied"))
    code, report = run_main(["104.245.103.27", "--apply", "--sg", "sg-b"], client, config_file(CONFIG))
    assert code == 1
    assert report["groups"][0]["rules"][0] == {"rule": "all traffic", "state": "error", "error": "AccessDenied"}


def test_a_missing_group_is_reported_for_that_group_only(config_file):
    client = FakeEc2([g for g in groups() if g["GroupId"] == "sg-b"])
    code, report = run_main(["104.245.103.27", "--apply"], client, config_file(CONFIG))
    assert code == 1
    assert report["groups"][0] == {
        "group_id": "sg-a",
        "group_name": None,
        "rules": [],
        "error": "InvalidGroup.NotFound: The security group 'sg-a' does not exist",
    }
    assert [r["state"] for r in report["groups"][1]["rules"]] == ["added"]


def test_ports_and_ipv6_follow_the_operator_configuration(config_file):
    client = FakeEc2([{"GroupId": "sg-a", "GroupName": "x", "IpPermissions": [
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "Ipv6Ranges": [{"CidrIpv6": "2001:4860:4860::8888/128"}]}
    ]}])
    path = config_file({"groups": ["sg-a"], "permissions": "tcp:22, tcp:8000-8100"})
    code, report = run_main(["2001:4860:4860::8888", "--apply"], client, path)
    assert code == 0
    assert [(r["rule"], r["state"]) for r in report["groups"][0]["rules"]] == [("tcp 22", "already_present"), ("tcp 8000-8100", "added")]
    assert client.authorized[0][1][0]["Ipv6Ranges"][0]["CidrIpv6"] == "2001:4860:4860::8888/128"
    assert client.authorized[0][1][0]["FromPort"] == 8000


def test_the_guard_rails_cannot_be_moved_by_environment_variables(config_file):
    client = FakeEc2(groups())
    environ = {"ARI_SG_ALLOWLIST_GROUPS": "sg-evil", "ARI_SG_ALLOWLIST_PERMISSIONS": "tcp:0-65535"}
    code, report = run_main(["104.245.103.27", "--sg", "sg-evil"], client, config_file(CONFIG), environ)
    assert code == 2
    assert "not in the configured groups: sg-evil" in report["error"]


def test_the_bundled_skill_ships_without_a_configuration():
    assert allow_ip.CONFIG_FILE == BUNDLED / "aws-security-group-allowlist" / "config.json"
    assert not allow_ip.CONFIG_FILE.exists()


@pytest.mark.parametrize(
    "args, config, message",
    [
        (["10.0.0.5"], CONFIG, "not a public address"),
        (["104.245.103.0/24"], CONFIG, "only a single address"),
        (["not-an-ip"], CONFIG, "is not an IP address"),
        (["104.245.103.27"], None, "does not exist, so no security group may be changed"),
        (["104.245.103.27"], "{not json", "cannot read"),
        (["104.245.103.27"], {"groups": []}, "must list the allowed security group ids"),
        (["104.245.103.27"], {"groups": ["sg-a"], "permissions": 22}, "must be text"),
        (["104.245.103.27", "--sg", "sg-zzz"], CONFIG, "not in the configured groups: sg-zzz"),
        (["104.245.103.27"], {**CONFIG, "permissions": "icmp:1"}, "is not 'all'"),
        (["104.245.103.27"], {**CONFIG, "permissions": "tcp:9-1"}, "outside 0-65535"),
    ],
)
def test_requests_outside_the_guard_rails_are_refused_before_aws_is_called(args, config, message, config_file, tmp_path):
    def no_client(region):
        raise AssertionError("AWS must not be called")

    path = config_file(config) if config is not None else tmp_path / "absent.json"
    out = io.StringIO()
    with redirect_stdout(out):
        code = allow_ip.main(args, environ={}, client_factory=no_client, now=NOW, config_path=path)
    assert code == 2
    assert message in json.loads(out.getvalue())["error"]


def test_a_missing_boto3_is_a_clear_refusal(monkeypatch, config_file):
    monkeypatch.setitem(sys.modules, "boto3", None)
    out = io.StringIO()
    with redirect_stdout(out):
        code = allow_ip.main(["104.245.103.27"], environ={}, now=NOW, config_path=config_file(CONFIG))
    assert code == 2
    assert json.loads(out.getvalue()) == {"error": "boto3 is not installed in this environment"}
