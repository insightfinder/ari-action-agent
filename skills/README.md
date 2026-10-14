# Skills

Each directory here is an [Agent Skill](https://agentskills.io/specification): a `SKILL.md` file with YAML frontmatter, plus optional `scripts/`, `references/` and `assets/`. The worker image ships them at `/app/skills`. The `run_skill` step runs one of them on its own, and the change and verify agents can load them during a code change; [docs/SKILLS.md](../docs/SKILLS.md) has the limits.

| Skill | Needs a repository | What it does |
|---|---|---|
| `tls-certificate-check` | no | Reports whether HTTPS endpoints have a valid certificate and when it expires. |
| `aws-security-group-allowlist` | no | Adds one public IP address to operator-configured AWS security groups. Refuses to change anything until the operator adds a `config.json` listing the allowed groups, and gives the worker AWS credentials and `boto3`. |
| `changelog-entry` | yes | Records a change in the repository's `CHANGELOG.md` while the agent makes a code change. |

A skill that needs no repository runs on its own through the `run_skill` step. A skill about code is used by the change agent inside a normal code-change request.

To add your own skill, put a directory next to these, or mount one as a subfolder of `SKILLS_DIR` (`SKILLS_DIR/<name>/SKILL.md`, default `/var/lib/ari-agent/skills`) without rebuilding the image. Check it first:

```
ari skills validate path/to/my-skill
```

[docs/SKILLS.md](../docs/SKILLS.md) covers the format, how the agent loads skills, and what a skill's scripts are allowed to do.
