from .base import StepContext


async def run(ctx: StepContext) -> dict:
    if ctx.config.get("options", {}).get("skip_tests"):
        return {"status": "skipped"}

    opencode = ctx.tool("opencode")
    instruction = ctx.params.get("instruction") or (
        "Run this project's build/lint/test suite (discover the right commands "
        "from the repo — package.json, pyproject.toml, Makefile, pom.xml, "
        "build.gradle, etc.). If it fails because of an unresolvable dependency, "
        "check whether a matching artifact is vendored in the repo itself (e.g. "
        "a .jar in the repo root or a lib/ directory whose name/version matches "
        "the missing dependency) — this is a common pattern for internal "
        "packages that were never published to a public registry, only ever "
        "installed by hand into a developer's local package cache. If you find "
        "one, install it into the local cache yourself (e.g. `mvn "
        "install:install-file` for Maven, the npm/pip equivalent otherwise) and "
        "retry the build before concluding failure. "
        "End your response with a line reading exactly 'RESULT: PASS' if everything "
        "succeeded, or 'RESULT: FAIL' otherwise, followed by the failure output."
    )
    result = await opencode(ctx, instruction)
    text = result["text"]
    if "RESULT: PASS" in text:
        return {"status": "passed", "trace": result["trace"]}
    return {"status": "failed", "failure": text, "trace": result["trace"]}
