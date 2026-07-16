# Railmux roadmap

This file records design candidates, not release commitments.

## Under discussion

### De-nested agent pane rendering

Prototype replacing the right-side nested `tmux attach-session` client with the
real agent pane. The likely mechanism is a tracked placeholder plus
`swap-pane`: move the selected agent pane from its detached background session
into the Railmux display slot, then swap it back before displaying another
agent. This should remove one PTY, terminal parser, and tmux composition pass
from every agent update while keeping the agent process owned by tmux rather
than by the Railmux Python process.

This is primarily a responsiveness project, not just an internal refactor.
Codex over the same SSH connection should feel close to a directly launched
Codex: the first wheel input should paint without a fixed 500 ms delay, a burst
should render only the newest useful viewport at roughly 20--30 FPS, and
scrolling must stop promptly when input stops instead of replaying queued
intermediate frames. Benchmark the current nested path, the prototype, and a
direct Codex baseline at the same pane size and SSH link before choosing the
default frame budget.

The low-risk scheduling step is implemented independently of pane migration:
copy-mode now renders the leading wheel update immediately while retaining the
existing conservative 500 ms frame for the remainder of a burst. De-nesting
and a faster adaptive or user-configurable frame remain prototype work and
require measurements plus the lifecycle safeguards below.

Lifecycle invariants for the prototype:

- Detached agent sessions remain the source of process persistence; removing
  the nested display client must not make an agent a child of Railmux.
- Graceful close and soft restart swap every displayed agent back to its home
  session before the sidebar exits.
- Persist enough pane/home/placeholder identity to recover after SIGKILL and
  return a stranded agent pane on the next launch without killing it.
- Switching, closing a display slot, transcript preview, terminal placement,
  F9, and future dual-agent layouts must never kill the background agent.
- Refuse or safely fall back to nested attach when the agent session has an
  independent attached client or its pane topology is not the supported
  single-agent shape.
- Keep scroll routing scoped to marked agent panes. Evolve the current fixed
  500 ms frame toward a configurable/adaptive 33--50 ms interval only after
  measurements justify it; disabling coalescing remains a diagnostic fallback,
  not the intended performance solution.

Open questions:

- Whether `swap-pane` preserves acceptable agent geometry across every switch,
  especially for long inline Codex transcripts.
- How placeholder ownership composes with two simultaneously visible agent
  slots without allowing one pane to appear in two places.
- Which tmux versions have sufficiently reliable cross-session pane swaps and
  which versions must retain the nested-client fallback.
- How much Claude Code improves when de-nested, since its alternate-screen,
  application-owned mouse path cannot use Codex's copy-mode batching unchanged.
