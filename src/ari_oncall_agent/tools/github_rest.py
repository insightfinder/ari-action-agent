"""Direct GitHub REST API client for the one write `publish_pr` needs —
opening a pull request. Deliberately not the GitHub MCP server this agent
used to route through (see docs/design.md §5): every piece of information
here (title, body, repo, branch names) is already fully deterministic by
the time this runs — generate_change already produced the summary and
file list — so there was no actual decision for an LLM to make. Routing
it through opencode + MCP bought nothing but an extra subprocess, several
LLM-mediated tool-call round trips, and an extra network hop, confirmed
live as the dominant cost in this step.
"""

import httpx


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
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API create pull request failed ({resp.status_code}): {resp.text[:2000]}")
        data = resp.json()

    return {"url": data["html_url"], "branch": head}
