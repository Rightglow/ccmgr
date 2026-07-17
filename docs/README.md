# Railmux design documentation

This directory is written primarily for coding agents starting with repository
context only. It contains the durable engineering decisions and runtime
evidence needed to make a correct change. It is not a home for implementation
prompts, one-off review reports, or completed task checklists; Git history and
issues already preserve those artifacts.

Read the smallest relevant document before changing behavior:

- [`ARCHITECTURE.md`](ARCHITECTURE.md) is the authoritative set of invariants
  for providers, restart and orphan recovery, session indexing, agent
  workspaces, display transports, layout, focus colours, lifecycle state, and
  attention state. Update it whenever a change alters one of those contracts.
- [`DENESTED_AGENT_PANE.md`](DENESTED_AGENT_PANE.md) records the evidence,
  transaction model, fallbacks, benchmarks, product decision, and unresolved
  limitations for the experimental swap transport. Keep measurements explicit
  about what they do and do not prove.
- [`BACKGROUND_SESSION_INDEX.md`](BACKGROUND_SESSION_INDEX.md) records the
  reproducible evidence behind the background Codex index. It supplements the
  immutable-generation rules in `ARCHITECTURE.md`.

Related repository-level documents have different roles:

- [`../ROADMAP.md`](../ROADMAP.md) contains candidates and open product
  questions, not implementation commitments.
- [`../CHANGELOG.md`](../CHANGELOG.md) records user-visible changes.
- [`../README.md`](../README.md) documents supported user workflows and should
  not carry internal design detail.

## Documentation policy

- Preserve decisions, constraints, recovery authority, compatibility limits,
  reproducible evidence, and unresolved risks.
- Prefer updating an existing authoritative document over adding overlapping
  design notes.
- Move a proven roadmap item into the appropriate architecture/evidence
  document; leave only genuine follow-up questions in the roadmap.
- Delete completed task prompts, generated diffs, and temporary review reports
  once their durable conclusions are represented here and the implementation
  is committed.
- Do not present mocked or synthetic measurements as real terminal, SSH, NFS,
  provider, or platform evidence.

## Document lifecycle

- Architecture invariants are long-lived, but should stay concise and describe
  the current contract rather than narrating how it was discovered.
- Evidence documents live while a product decision remains open, an
  experimental path remains supported, or their reproduction steps still
  protect against a wrong future decision. Prune superseded experiments instead
  of accumulating an implementation diary.
- When an experimental decision closes, move its lasting contract and
  compatibility rules into `ARCHITECTURE.md`, move genuine follow-up questions
  into `ROADMAP.md`, retain reproducible tools when they still have value, and
  then shorten or delete the evidence document. Git history preserves the full
  investigation.
- Completed task specifications, generated diffs, and review handoff files are
  disposable once their durable conclusions and unresolved risks have reached
  the authoritative documents.

`DENESTED_AGENT_PANE.md` is still active because swap rendering remains
experimental and opt-in, and real-provider paint/SSH/reflow evidence still
gates its default or removal decision. It should not remain a long historical
report forever: once that decision closes, distill the final transport,
fallback, recovery, and compatibility contract into `ARCHITECTURE.md`, preserve
only useful reproducible benchmarks, and substantially shorten or remove the
evidence narrative.
