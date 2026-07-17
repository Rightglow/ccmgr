# De-nested agent pane feasibility and experiment record

This document records the evidence behind Railmux's `swap` display transport.
Lifecycle safety is covered on Linux with tmux 2.7 and 3.4 and by the Linux and
macOS private-tmux CI matrix. A reproducible local server-side benchmark shows
a narrow pipeline improvement on tmux 3.4, while field use found session
switching more responsive and exposed recovery/selection defects that were
fixed before the 0.1.2 default change. The measurements still do not claim to
observe remote client paint or every provider workload.

## Result

The prototype is functionally feasible when all of these conditions hold:

- Railmux owns the auto-launched outer session named `railmux`.
- tmux is 2.7 or newer.
- The agent session is detached and has exactly one live pane in one window.
- No other `AgentSlot` or marked transaction owns the real pane.
- The transaction's pane, window, session, PID, and marker identities all
  validate before and after the swap.

Anything else uses the unchanged nested `tmux attach-session` transport. Swap
is the default preference; users can explicitly set
`[live] agent_transport = "nested"` to force the compatibility path.

## Lifecycle experiments

All local experiments used a private short tmux socket, `/dev/null` config,
fixed-size detached sessions, and cleanup traps. The implementation repeats the
critical cases in `tests/test_tmux_integration.py`.

1. **Pane state.** A cross-session swap on tmux 3.4 kept the pane ID and PID,
   process environment, cwd, 64 lines of captured history, and
   `alternate_on=1`. The pane adopted the display geometry (83x27 became
   84x40), as tmux layout semantics require. The tmux *session environment* did
   not move; the already-running process environment did.
2. **Persistent home.** The display placeholder moved into the agent's home
   window, leaving one live pane there and therefore keeping the home session
   alive while the real pane was displayed.
3. **A -> B -> A.** Both real pane IDs and PIDs remained stable and each pane
   returned to its recorded home window before the other was displayed.
4. **Controlled lifecycle.** Normal close, preview, soft quit, and hard delete
   were exercised by returning real panes first. Preview respawned only the
   returned display placeholder. Deleting A killed A's real home process while
   B remained alive.
5. **Python SIGKILL recovery.** Versioned JSON records in slot-specific tmux
   window user options located the real pane, home window, display placeholder,
   keeper, outer session, and Railmux owner pane without Python state. Recovery
   repaired both a stranded displayed pane and an already-home interrupted
   transaction. If the marked placeholder/home session disappeared, recovery
   recreated only that exact absent marked session and returned the recorded
   real pane; it never adopts an unmarked pane.
6. **Direct outer-session kill.** A detached tmux session group created with
   `new-session -d -t railmux -s <keeper>` shares the display window without an
   extra pane or PTY. Killing `railmux` left the window, real pane, and PID alive
   under the keeper. Recovery swapped the agent home before removing the
   keeper. This passed on installed tmux 3.4 and a locally built upstream tmux
   2.7.
7. **Independent client.** A second real tmux client produced
   `session_attached=1`. The implementation stabilizes any old Railmux nested
   pane, waits for its client to detach, then rechecks. A remaining independent
   client selects nested fallback and is not resized or swapped.
8. **Unsupported topology.** A two-window, three-pane agent session was
   rejected. Exact window and pane counts come from server queries, not a
   session-name convention.
9. **Platforms and versions.** The full implementation smoke passed on Linux
   with tmux 3.4 and upstream tmux 2.7 built from its official release tarball.
   tmux 2.7 supports the required cross-session `swap-pane`, window user
   options, session grouping, and immutable IDs. It does not have
   `resize-window`, so de-nested geometry changes may visibly reflow content on
   2.7/2.8. The existing CI smoke matrix runs the same private-socket tests on
   Linux and macOS; the macOS result must be confirmed after an approved push.
10. **Two slots.** Two distinct placeholders displayed A and B concurrently,
    then returned both home with unchanged PIDs. `AgentWorkspace` and durable
    transaction ownership reject a second claim on the same real pane. The
    public UI still exposes only primary, but transport ownership is slot-keyed
    for primary and secondary.

## Transaction and recovery model

Before movement, the transport validates all identities, establishes the
session-group keeper, and writes the same `prepared` record to the home and
display windows. It swaps the recorded pane IDs, verifies PID, window
locations, topology, and attached-client count, then writes `displayed`.

Returning home writes `returning`, swaps the same IDs, verifies the real pane
in its recorded home, clears only markers with the matching transaction ID,
and drops the keeper when no slot remains displayed. A failure after movement
first attempts a verified rollback. If rollback cannot be proven, the
transport retains the marker and keeper and refuses destructive fallback.
Startup recovery treats repository/session names as insufficient evidence;
only a structurally valid marker plus matching immutable tmux identities can
authorize repair.

The placeholder runs a portable long-lived shell loop. A transcript viewer or
nested client is always respawned back to that stable placeholder before it may
be exchanged into an agent's home.

## Fallback conditions

Nested display is selected when it is explicitly configured, tmux is too old,
the outer session is not the managed `railmux` session, identity probes fail,
an independent client remains attached, topology is not one live pane/window,
a real pane is already owned by another slot, keeper creation or metadata
persistence fails, or a pre-movement swap command fails. A
post-movement failure with unproven rollback fails closed and leaves recovery
metadata rather than respawning or killing either recorded pane.

## Performance observations

### Reproducible Phase 6 benchmark

Run the source-tree-only harness (it is not installed in the wheel) with:

```bash
python tools/denested_transport_benchmark.py \
  --runs 7 --lines 2500 --line-width 96 \
  --columns 112 --rows 40 --switch-iterations 20 \
  --output denested-results.json
```

The harness creates a mode-0700 short directory under `/tmp`, starts a private
tmux server with an explicit `-S` socket on every command, and destroys only
that server in `finally`. It never uses the ambient `TMUX` socket. Its JSON
contains the environment, dataset, measurement scope, raw samples, summaries,
and a deterministic scheduling model.

The output metric begins before a synthetic producer command is dispatched and
ends when `capture-pane` on the private server contains its final marker. It
includes command dispatch, producer startup/generation, tmux processing, and
2 ms polling. It does **not** observe SSH packets or paint in the user's local
terminal. Likewise, switch timing is server-command plus identity-observation
latency, not a perceived UI switch. The sought marker is read from a private
out-of-band file and shell input echo is disabled, so the typed producer command
cannot satisfy the marker check before the final output is emitted.

On 2026-07-17, three independent batches ran on Linux 6.11, Python 3.12.3,
tmux 3.4, 112x40 geometry, with an SSH environment detected. Each batch used
seven 2,500-line bursts per path and 20 A/B switches per transport:

| Batch | Direct marker median | Nested marker median | Swap marker median | Nested switch median | Swap switch median |
|---|---:|---:|---:|---:|---:|
| 1 | 58.980 ms | 70.147 ms | 58.000 ms | 9.848 ms | 8.468 ms |
| 2 | 56.933 ms | 69.306 ms | 56.984 ms | 10.333 ms | 8.532 ms |
| 3 | 57.839 ms | 70.005 ms | 58.279 ms | 10.099 ms | 8.566 ms |

Raw marker-observation samples, in milliseconds:

- Batch 1 — direct `[77.364, 57.036, 63.053, 62.917, 56.495, 58.980, 56.380]`; nested `[78.734, 73.178, 69.309, 70.147, 67.923, 67.279, 75.849]`; swap `[59.954, 57.745, 59.065, 58.360, 56.346, 57.489, 58.000]`.
- Batch 2 — direct `[68.228, 56.665, 59.198, 56.933, 57.649, 56.437, 53.814]`; nested `[75.794, 80.427, 69.306, 68.404, 69.959, 68.120, 69.057]`; swap `[56.984, 59.254, 58.975, 56.124, 56.368, 56.735, 57.535]`.
- Batch 3 — direct `[74.184, 58.929, 57.387, 57.600, 57.999, 57.827, 57.839]`; nested `[75.777, 73.880, 69.717, 69.910, 70.005, 70.414, 67.847]`; swap `[58.178, 59.105, 59.173, 58.279, 56.910, 59.676, 57.189]`.

All observations retained the requested 112x40 pane/window geometry. Direct
used one path pane; nested and swap each used two, because swap replaces the
visible nested client with a hidden home placeholder rather than eliminating a
PTY. The consistent marker gap shows that the nested client adds measurable
work to this local synthetic server pipeline, while swap remains close to the
direct path. This supports the default product choice together with field use
and lifecycle coverage; it is not evidence of first-visible-paint improvement.

Linux `/proc` CPU totals use a 100 Hz clock and include polling overhead. Across
the three batches, tmux-server ticks were direct `15/14/13`, nested `24/23/22`,
and swap `12/12/14`; synthetic producer-tree ticks were indistinguishable
(`12`--`14`). Nested-client totals were below one clock tick and render as zero,
which means “below 10 ms resolution,” not zero CPU. Railmux and real-provider
CPU were not present in this harness and were not inferred.

The deterministic 31-event/240 ms scheduler trace produced:

| Policy | Modeled updates | Leading delay | Modeled tail delay |
|---|---:|---:|---:|
| Disabled | 31 | 0 ms | 0 ms |
| Fixed 100 ms (current) | 4 | 0 ms | 60 ms |
| Fixed 50 ms | 6 | 0 ms | 10 ms |
| Fixed 33 ms | 9 | 0 ms | 24 ms |
| Adaptive prototype | 9 | 0 ms | 24 ms |

Tail values depend on where the burst ends relative to a deadline; the fixed
policies bound tail by their interval. These are scheduler decisions, not
measured frames. They show the expected update-count/tail tradeoff but do not
justify a new setting, an adaptive implementation, or changing the conservative
100 ms default without real terminal-paint evidence.

### Earlier feasibility smoke

The original prototype smoke used the same 112x40 shape and seven 2,500-line
bursts, but its one-off harness was not retained. Marker arrival included
generation, tmux processing, pipe capture, and 5 ms polling:

| Path | Median burst marker | Max | Median tmux server CPU ticks |
|---|---:|---:|---:|
| Nested | 36.24 ms | 76.68 ms | 1 |
| Swap | 42.51 ms | 87.02 ms | 1 |
| Direct | 40.40 ms | 46.20 ms | 1 |

Twenty A/B switches measured 10.35 ms median (11.49 ms p95) for nested
respawn/attach and 5.48 ms median (6.83 ms p95) for two `swap-pane` commands.
CPU resolution was too coarse to distinguish the paths. That earlier smoke did
not demonstrate a useful overall responsiveness gain. The reproducible Phase 6
run does show a consistent narrow server-pipeline gain; neither run measures
perceived responsiveness.

The benchmark environment could not validly measure first remote wheel paint,
queued-frame drain after a wheel burst, real Claude/Codex sustained output,
clipboard/mouse behavior through an actual terminal client, or long inline
Codex transcript resize over the same SSH link. These remain useful follow-up
measurements, not claims made by the default change.

## Product decision for 0.1.2

Use `swap` as the default preference, with every existing validation gate and
automatic nested fallback retained. This is a product decision based on the
combined lifecycle tests, cross-platform isolated-tmux CI, synthetic evidence,
and real interactive field use; the benchmark alone would not justify it.
`nested` remains supported as an explicit compatibility setting.

The decision remains falsifiable. Reproducible data loss, pane-identity errors,
or a material provider regression should first disable swap for the affected
environment through a narrow capability gate; a broad regression across both
providers should restore nested as the default rather than normalize transport
complexity. Optional client-paint measurements can still refine performance
claims and scroll policy without weakening lifecycle safety.

## Unresolved limitations

- tmux 2.7/2.8 cannot pre-size a home window with `resize-window`; switching
  uses native tmux reflow and may be visibly disruptive for a long inline TUI.
- A user who directly kills the *real display pane* still kills that provider;
  controlled Railmux close/preview/quit paths always return it home first.
- Synthetic marker and switch timings are not evidence that Claude or Codex
  feels faster over SSH or that a local terminal painted sooner.
- The public dual-agent layout and its focus/border interaction remain separate
  roadmap work even though transport ownership is two-slot safe.
