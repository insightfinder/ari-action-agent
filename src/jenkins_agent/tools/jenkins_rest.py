"""Direct REST fallback for the one mutating operation the Jenkins MCP
plugin's own documented tool list (https://plugins.jenkins.io/mcp-server/)
doesn't clearly include: stopping a running build (the plugin lists
`triggerBuild`/`rebuildBuild`/`replayBuild`/`updateBuild`, no obvious
stop/cancel tool). `execute_action.py` uses this for a "stop" action and
opencode+MCP for a "trigger" action.

Not yet verified against a real Jenkins instance — in particular:
- Whether the MCP plugin actually does expose a stop tool under a name
  this module's author didn't find in the plugin's own docs page, which
  would make this fallback unnecessary.
- The exact folder-path URL convention for nested/multibranch jobs
  (assumed here to be repeated "/job/<segment>" per path segment, Jenkins'
  own long-documented convention, but not exercised against a real
  multibranch job).
- Whether the target instance's CSRF crumb issuer is enabled/required —
  fetched and attached when available; harmless if the instance ignores it.
"""

import base64

import httpx


def _auth_header(auth: dict) -> str:
    auth_type = auth.get("type", "api_token")
    if auth_type in ("api_token", "basic"):
        raw = f"{auth['user']}:{auth['token']}".encode()
        return f"Basic {base64.b64encode(raw).decode()}"
    if auth_type == "bearer":
        return f"Bearer {auth['token']}"
    raise ValueError(f"unsupported jenkins auth.type: {auth_type!r}")


def job_url(base_url: str, job_full_name: str) -> str:
    """"folder/subfolder/job-name" -> Jenkins' own nested-job URL
    convention, one "/job/<segment>" per path segment."""
    segments = "/job/".join(part for part in job_full_name.split("/") if part)
    return f"{base_url.rstrip('/')}/job/{segments}"


async def stop_build(jenkins: dict, job_full_name: str, build_number: int) -> None:
    base_url = jenkins["base_url"]
    headers = {"Authorization": _auth_header(jenkins["auth"])}
    verify = jenkins.get("verify_tls", True)
    if jenkins.get("ca_bundle"):
        verify = jenkins["ca_bundle"]  # httpx accepts a CA bundle path/content for `verify`

    async with httpx.AsyncClient(verify=verify, timeout=30) as client:
        crumb_resp = await client.get(f"{base_url.rstrip('/')}/crumbIssuer/api/json", headers=headers)
        if crumb_resp.status_code == 200:
            crumb = crumb_resp.json()
            headers[crumb["crumbRequestField"]] = crumb["crumb"]

        resp = await client.post(f"{job_url(base_url, job_full_name)}/{build_number}/stop", headers=headers)
        resp.raise_for_status()
