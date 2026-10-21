---
name: changelog-entry
description: Records a code change in the repository's CHANGELOG.md following the Keep a Changelog format. Use while making a change in a repository that already has a CHANGELOG.md, or when the request asks for a changelog or release-notes entry.
metadata:
  author: insightfinder
  version: "1.0"
---

# Changelog entry

Use this after the code change itself is done, as part of the same change.

## When to add an entry

- The repository has a `CHANGELOG.md` at its root, or the request explicitly asks for a changelog entry.
- Do not create a new `CHANGELOG.md` unless the request asks for one.
- Never rewrite or reorder existing entries.

## Where it goes

Keep a Changelog keeps unreleased work in a section at the top:

```
## [Unreleased]

### Fixed
- Stop the payment retry loop from double-charging on timeouts.
```

1. Find the `## [Unreleased]` heading. If it does not exist, add it directly above the newest released version heading, or below the file's introduction when there are no versions yet.
2. Under it, use the subsection that matches the change. Create it if it is missing, in this order: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.
3. Add one bullet at the end of that subsection.

## How to write the bullet

- One line, in the past or imperative tense the file already uses, describing the effect for a user of the project, not the implementation.
- Mention the issue or ticket id when the request gives one, in the style the file already uses.
- Match the file's existing punctuation, capitalisation and link style.

If the file clearly follows a different format, follow that format instead of Keep a Changelog.
