"""Direct GitHub REST API client for the one write `publish_pr` needs —
opening a pull request. Deliberately not the GitHub MCP server this agent
used to route through (see docs/design.md §5): every piece of information
here (title, body, repo, branch names) is already fully deterministic by
the time this runs — generate_change already produced the summary and
file list — so there was no actual decision for an LLM to make. Routing
it through opencode + MCP bought nothing but an extra subprocess, several
LLM-mediated tool-call round trips, and an extra network hop, confirmed
live as the dominant cost in this step.

Opening a PR is not idempotent, and it is the one call here that retrying
could duplicate, so retries are paired with an explicit identity for the
thing being created: the head branch. `publish_pr._branch_name` derives
that branch deterministically from the request id, so every attempt of one
request — an httpx-level retry inside `request_with_retry`, a Temporal
re-run of the whole capability, or an operator re-submitting — asks for a
PR on the *same* branch. That makes "does this already exist?" answerable,
which is what the lookup below is for. Without it, a 502 arriving after
GitHub had already created the PR turned every subsequent attempt into a
422 the caller reported as an outright failure.
"""

import httpx

from common.http_retry import request_with_retry


async def _find_open_pull_request(
    client: httpx.AsyncClient, headers: dict, owner_repo: str, head: str
) -> dict | None:
    """The open PR for `head`, or None.

    Best-effort on purpose: anything other than a clean 200 carrying a
    non-empty list is treated as "no existing PR" and lets the caller go
    on to create one. This lookup exists to *avoid* a duplicate, so it
    must never be the reason a PR does not get opened at all.
    """
    owner = owner_repo.split("/")[0]
    response = await request_with_retry(
        client,
        "GET",
        f"https://api.github.com/repos/{owner_repo}/pulls",
        what=f"GitHub API list pull requests for {owner_repo}",
        headers=headers,
        params={"head": f"{owner}:{head}", "state": "open"},
    )
    if response.status_code != 200:
        return None
    try:
        existing = response.json()
    except ValueError:
        return None
    if not isinstance(existing, list) or not existing:
        return None
    return existing[0]


async def create_pull_request(repo_config: dict, owner_repo: str, title: str, body: str, head: str, base: str) -> dict:
    if not repo_config.get("pat"):
        raise RuntimeError("github_rest.create_pull_request requires repo.pat — the REST API needs a token")

    url = f"https://api.github.com/repos/{owner_repo}/pulls"
    headers = {
        "Authorization": f"Bearer {repo_config['pat']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "head": head, "base": base}

    async with httpx.AsyncClient(timeout=30) as client:
        already = await _find_open_pull_request(client, headers, owner_repo, head)
        if already:
            return {"url": already["html_url"], "branch": head}

        resp = await request_with_retry(
            client,
            "POST",
            url,
            what=f"GitHub API create pull request for {owner_repo}",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 400:
            # The race the lookup above cannot close: a previous attempt's
            # POST reached GitHub and created the PR, but its response was
            # lost, so this attempt still saw no open PR. GitHub reports
            # that as 422, and the PR we were asked to open now exists —
            # which is the outcome the caller wanted.
            if resp.status_code == 422:
                created_meanwhile = await _find_open_pull_request(client, headers, owner_repo, head)
                if created_meanwhile:
                    return {"url": created_meanwhile["html_url"], "branch": head}
            raise RuntimeError(f"GitHub API create pull request failed ({resp.status_code}): {resp.text[:2000]}")
        data = resp.json()

    return {"url": data["html_url"], "branch": head}
