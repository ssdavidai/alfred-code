---
description: "Emit a functional smoke-test template for a recurring lane shape"
allowed-tools: Read
argument-hint: "<kind>  (channel-token | workflow-trigger | migration-roundtrip | mcp-server-tool | wasp-op)"
---

Read the template at `smoke-templates/$1.md` (relative to the alfred-code install
root — likely `~/.claude/alfred-code/smoke-templates/$1.md` or
`~/.claude/plugins/alfred-code/smoke-templates/$1.md`).

Emit it inline, ready to paste into the current lane agent's prompt as the
**REAL FUNCTIONAL SMOKE TEST** section. The template will have placeholders like
`<SLUG>` and `<MIGRATION_NUM>` — leave them as-is; the lane agent fills them in.

If `$1` doesn't match a known template, list available templates and stop:
```
!`ls ~/.claude/alfred-code/smoke-templates/*.md ~/.claude/plugins/alfred-code/smoke-templates/*.md 2>/dev/null | xargs -n1 basename | sort -u`
```

The emitted template MUST include these standard sections:
- §1 Baseline assertion (no-regression for main)
- §2 Setup (create test profile / upload fixture / etc.)
- §3 Mutation (the actual route / workflow / tool we're testing)
- §4 Isolation assertion (no clobber across profiles / no main affected)
- §5 Audit-row presence check
- §6 Cleanup (no orphans on home)

These sections are non-negotiable. Tonight's `tini -g` bug + the AgentMail
master-key gap were both caught by isolation assertions, not by lint.
