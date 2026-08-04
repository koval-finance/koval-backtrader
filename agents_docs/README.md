# agents_docs — knowledge index

Purpose: the index of the agent-facing documentation. The root
[AGENTS.md](../AGENTS.md) holds what every task needs; these files hold
depth. Load only what the task calls for.

## Files

| File | Owns |
|---|---|
| [invariants.md](invariants.md) | every load-bearing rule and the test that pins it |
| [agent_workflow.md](agent_workflow.md) | the work loop, the git policy, the hard rules |
| [architecture.md](architecture.md) | the plugin seam, the four modules, the run path |
| [testing.md](testing.md) | suite layout, fixtures, what "tested" means here |
| [release_process.md](release_process.md) | version truth, changelog gate, the tag pipeline |
| [troubleshooting.md](troubleshooting.md) | dated failure memory |

## Task routing

| Task | Read first |
|---|---|
| Anything touching licensing or the entry point | [invariants.md](invariants.md) — before writing code |
| Change how orders, fills, or exits behave | [architecture.md](architecture.md), then [testing.md](testing.md) |
| Upgrade the Backtrader dependency | [invariants.md](invariants.md), then [troubleshooting.md](troubleshooting.md) |
| Test failure or unexpected behaviour | [troubleshooting.md](troubleshooting.md), then [testing.md](testing.md) |
| Cutting a release | [release_process.md](release_process.md) |

## Cold-session reading order

1. [AGENTS.md](../AGENTS.md)
2. This index
3. The routed file for your task

## Maintenance

One file owns each topic; cross-link rather than repeat. Every file ends with
an "Update this file when" note — honour it in the same change that makes it
true. Consistency between this index, the files, and the root entry file is
pinned by [`tests/test_agents_docs.py`](../tests/test_agents_docs.py).
