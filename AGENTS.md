# Railmux agent guidance

This repository's design documentation is written primarily for coding agents
starting with repository context only.

Before planning a non-trivial behavior or architecture change:

1. Read [`docs/README.md`](docs/README.md) to locate the authoritative source.
2. Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for every affected
   invariant, then read the relevant evidence document when one is listed.
3. Treat [`ROADMAP.md`](ROADMAP.md) as open questions, not approved behavior,
   and [`README.md`](README.md) as the user contract, not an internal design
   specification.

Do not use completed task prompts, generated diffs, or review transcripts as a
competing source of truth. When implementation changes a durable invariant,
compatibility boundary, recovery authority, or evidence-based product decision,
update the corresponding document in the same change.
Keep provider-neutral and multi-slot constraints intact unless the task
explicitly changes the documented architecture.

Follow [`CONTRIBUTING.md`](CONTRIBUTING.md) for verification and delivery.
