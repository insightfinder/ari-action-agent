"""Local, read-only test harness for the Jenkins agent's capabilities —
runs the exact same `common.capabilities_registry.run_capability` entrypoint
the real Temporal worker uses, but in-process: no Temporal, no A2A server,
no Helm deployment needed. Useful for confirming each capability actually
works against a real Jenkins before wiring up the full pipeline.

SAFETY: only wires four capabilities on purpose — resolve_target, inspect,
diagnose, propose_action. All four are read-only by construction (the
opencode agent behind them, "ari-jenkins-read", has every `jenkins_*`
mutating tool denied — see jenkins_agent/tools/opencode_cli.py). This
script deliberately does NOT expose execute_action or jenkins_stop_build;
don't extend it to do so against a production Jenkins without separately
verifying the tool-name-pattern gap described in `discover-tools` below.

Before trusting ANY of the above, run `discover-tools` once against the
real Jenkins and read the debug log yourself. The `jenkins_*` deny pattern
is only a guess (by analogy with GitHub's MCP server's `github_*` prefix,
confirmed live; Jenkins's own prefix has never been confirmed). opencode
runs with `--auto` (auto-approve anything not explicitly denied), so if
the real prefix differs, the deny rule silently matches nothing and a
mutating tool would go through auto-approved. `discover-tools` prints
opencode's own DEBUG-level tool registration log lines so you can check
the real names against the code's `jenkins_*` assumption yourself — this
script does not (and cannot reliably) verify that for you.

Usage:
    export JENKINS_BASE_URL=https://jenkins.internal.example.com
    export JENKINS_USER=ari-jenkins-agent-readonly
    export JENKINS_TOKEN=...              # API token for that user
    export LLM_API_KEY=...                # same provider key as the on-call agent
    # optional: LLM_PROVIDER (default anthropic), LLM_MODEL, JENKINS_VERIFY_TLS
    # (default true), JENKINS_CA_BUNDLE_FILE (path to a PEM to inline)

    python scripts/jenkins_agent_local_test.py discover-tools

    python scripts/jenkins_agent_local_test.py resolve_target \
        --prompt "how's the checkout-service deploy job doing?"

    # Chain steps -- each --step's result becomes ctx.results for the next,
    # exactly like a real plan's sequential execution:
    python scripts/jenkins_agent_local_test.py \
        --prompt "why has the nightly build been red since Tuesday?" \
        --step resolve_target --step diagnose
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AGENT_KIND", "jenkins")

from common.capabilities_base import StepContext  # noqa: E402
from common.capabilities_registry import run_capability  # noqa: E402
from common.sandbox import cleanup_workspace, create_workspace  # noqa: E402
from jenkins_agent.tools.opencode_cli import _AGENT_DEFS, run_opencode  # noqa: E402

_READ_ONLY_STEPS = {"resolve_target", "inspect", "diagnose", "propose_action"}


def _jenkins_config() -> dict:
    ca_bundle = None
    if os.environ.get("JENKINS_CA_BUNDLE_FILE"):
        ca_bundle = Path(os.environ["JENKINS_CA_BUNDLE_FILE"]).read_text()
    return {
        "base_url": os.environ["JENKINS_BASE_URL"],
        "auth": {
            "type": os.environ.get("JENKINS_AUTH_TYPE", "api_token"),
            "user": os.environ.get("JENKINS_USER"),
            "token": os.environ["JENKINS_TOKEN"],
        },
        "verify_tls": os.environ.get("JENKINS_VERIFY_TLS", "true").lower() != "false",
        "ca_bundle": ca_bundle,
    }


def _llm_config() -> dict:
    return {
        "provider": os.environ.get("LLM_PROVIDER", "anthropic"),
        "api_key": os.environ["LLM_API_KEY"],
        "model": os.environ.get("LLM_MODEL", "claude-sonnet-5"),
    }


async def discover_tools() -> None:
    """Prints opencode's own tool-registration log lines for the real
    Jenkins MCP server, so you can check the actual tool name prefix
    against jenkins_agent/tools/opencode_cli.py's `jenkins_*` assumption
    yourself -- see this module's docstring for why that matters."""
    print(f"ari-jenkins-read permission block: {_AGENT_DEFS['ari-jenkins-read']['tools']}")
    print("(if the real tool names below don't start with 'jenkins_', that block isn't denying anything)\n")

    request_id = f"discover-{uuid.uuid4().hex[:8]}"
    workspace = create_workspace(request_id)
    try:
        ctx = StepContext(
            request_id=request_id,
            workspace=workspace,
            prompt="",
            config={"jenkins": _jenkins_config(), "llm": _llm_config()},
            capability="resolve_target",  # maps to ari-jenkins-read
            params={},
            results={},
            tools={},
        )
        from jenkins_agent.tools.opencode_cli import write_config

        write_config(ctx.workspace, ctx.config["llm"], ctx.config["jenkins"])

        instruction = (
            "List every tool available to you whose name relates to Jenkins, exactly as "
            "named in your tool schema, one per line as '<name>: <one-line purpose>'. Do "
            "NOT call any tool -- just report what's available."
        )
        result = await run_opencode(ctx, instruction)
        print("--- opencode's self-report (a hint, not ground truth) ---")
        print(result["text"])
        print()
        print(
            "For ground truth, rerun manually with --print-logs --log-level DEBUG and grep "
            "the stderr for the MCP server's own tool registration, e.g.:\n"
            f"  cd {workspace}\n"
            "  opencode run --dir . --agent ari-jenkins-read "
            f"--model {ctx.config['llm']['provider']}/{ctx.config['llm']['model']} "
            "--format json --print-logs --log-level DEBUG \\\n"
            "    'list your tools' 2>debug.log 1>/dev/null\n"
            "  grep -i 'jenkins\\|mcp\\|tool' debug.log | less"
        )
    finally:
        cleanup_workspace(request_id)


async def run_steps(steps: list[str], prompt: str) -> None:
    for step in steps:
        if step not in _READ_ONLY_STEPS:
            raise SystemExit(
                f"refusing to run {step!r}: this harness only wires read-only capabilities "
                f"({sorted(_READ_ONLY_STEPS)}). execute_action/jenkins_stop_build are "
                "deliberately not exposed here -- see this script's module docstring."
            )

    request_id = f"local-{uuid.uuid4().hex[:8]}"
    workspace = create_workspace(request_id)
    results: dict = {}
    try:
        for step in steps:
            ctx = StepContext(
                request_id=request_id,
                workspace=workspace,
                prompt=prompt,
                config={"jenkins": _jenkins_config(), "llm": _llm_config()},
                capability=step,
                params={},
                results=results,
                tools={},
            )
            print(f"\n=== {step} ===")
            result = await run_capability(step, ctx)
            print(json.dumps(result, indent=2, default=str))
            results[step] = result
    finally:
        cleanup_workspace(request_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode")
    sub.add_parser("discover-tools")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--step", action="append", dest="steps", default=[])
    args = parser.parse_args()

    if args.mode == "discover-tools":
        asyncio.run(discover_tools())
        return

    if not args.steps:
        raise SystemExit("pass at least one --step (resolve_target/inspect/diagnose/propose_action)")
    asyncio.run(run_steps(args.steps, args.prompt))


if __name__ == "__main__":
    main()
