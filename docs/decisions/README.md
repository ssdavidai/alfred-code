# Architecture Decision Records

This directory holds ADRs — short documents that capture a decision and the reasoning behind it. We file an ADR whenever:

- A Y/N gate from Sir produces a choice that other lanes will inherit
- A lane orchestrator picks one of two architectural shapes (e.g. cascade-unbind-on-archive vs resolver-side-fallback)
- A research lane (per the triage protocol) produces a recommendation that becomes load-bearing for future work

## Naming

`NNNN-kebab-case-title.md` where `NNNN` is the next free 4-digit number.

## Shape

Read `ADR-template.md`. Every ADR has:

1. **Title** — what the decision is, in one line
2. **Status** — proposed / accepted / superseded by NNNN / rejected
3. **Context** — what made the decision necessary
4. **Decision** — the actual choice + a short justification
5. **Consequences** — what gets easier, what gets harder, what we're now locked into
6. **Date** + **Decider** — when, by whom

## Workflow

- `/file-adr` slash command drafts a fresh ADR from a `/tmp/orchestrator-N-decision.md` snapshot
- The orchestrator creates `/tmp/orchestrator-N-decision.md` when surfacing a Y/N gate that's architecturally load-bearing
- Sir's tap on the gate triggers `/file-adr` to convert the gate's chosen option into a committed ADR

## Index

(auto-generated; run `/file-adr --reindex` to rebuild)

| # | Title | Status | Date |
|---|---|---|---|
