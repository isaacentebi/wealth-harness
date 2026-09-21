# Handoff

Start here when picking up Wealth.

1. [README.md](README.md): what the product does and its boundaries.
2. [docs/architecture.md](docs/architecture.md): module map, data flow,
   invariants and trust boundaries.
3. [docs/cli.md](docs/cli.md): every command and MCP tool with a working example.
4. [wealth/instructions.md](wealth/instructions.md): the conversation policy the
   model follows; [SKILL.md](SKILL.md) is its condensed form for MCP hosts and
   must not contradict it.
5. [docs/verification.md](docs/verification.md): how to check a change.

Area references: [docs/trading.md](docs/trading.md) (order execution and its
threat model), [docs/onboarding.md](docs/onboarding.md) (onboarding cards),
[docs/openclaw.md](docs/openclaw.md) (text channels),
[docs/mexico-investing-facts.md](docs/mexico-investing-facts.md) (verified
Mexico rules the engines cite).

Product direction: [docs/bar.md](docs/bar.md) (definition of done),
[docs/scope.md](docs/scope.md) (the full scope map),
[docs/ux-bar.md](docs/ux-bar.md) (interface rules). Research behind decisions:
[docs/teardown.md](docs/teardown.md), [docs/memory-research.md](docs/memory-research.md),
[docs/live-review-2026-09-21.md](docs/live-review-2026-09-21.md).

When the code changes what Wealth can do, update the README table, SKILL.md,
docs/architecture.md and docs/cli.md in the same change; `tests/test_docs.py`
catches task and tool names that drift.
