# De-nested agent pane feasibility and experiment record

This document records the evidence for Railmux's experimental `swap` display
transport. It is deliberately not the default: lifecycle safety is proven on
Linux with tmux 2.7 and 3.4, but real-provider responsiveness, SSH wheel
behavior, long Codex transcript reflow, and the macOS CI run still need enough
evidence to justify changing the default.

## Result

The prototype is functionally feasible when all of these conditions hold:

- Railmux owns the auto-launched outer session named `railmux`.
- tmux is 2.7 or newer.
- The agent session is detached and has exactly one live pane in one window.
- No other `AgentSlot` or marked transaction owns the real pane.
- The transaction's pane, window, session, PID, and marker identities all
  validate before and after the swap.

Anything else uses the unchanged nested `tmux attach-session` transport. The
user must explicitly set `[live] agent_transport = "swap"`; the default is
`nested`.

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

Nested display is selected when swap was not explicitly requested, tmux is too
old, the outer session is not the managed `railmux` session, identity probes
fail, an independent client remains attached, topology is not one live
pane/window, a real pane is already owned by another slot, keeper creation or
metadata persistence fails, or a pre-movement swap command fails. A
post-movement failure with unproven rollback fails closed and leaves recovery
metadata rather than respawning or killing either recorded pane.

## Performance observations

A local synthetic test used tmux 3.4, a 112x40 display, seven bursts of 2,500
lines, and the same shell producer for nested and swap paths. Marker arrival
included generation, tmux processing, pipe capture, and 5 ms polling, so these
numbers are comparative smoke data, not a provider latency claim:

| Path | Median burst marker | Max | Median tmux server CPU ticks |
|---|---:|---:|---:|
| Nested | 36.24 ms | 76.68 ms | 1 |
| Swap | 42.51 ms | 87.02 ms | 1 |
| Direct | 40.40 ms | 46.20 ms | 1 |

Twenty A/B switches measured 10.35 ms median (11.49 ms p95) for nested
respawn/attach and 5.48 ms median (6.83 ms p95) for two `swap-pane` commands.
CPU resolution was too coarse to distinguish the paths. Synthetic sustained
output did **not** demonstrate a useful overall responsiveness gain.

With two background agents the nested and swap setups each had four unique
tmux panes in the benchmark. Swap replaces the visible nested-client pane with
a hidden home placeholder, so total PTY count does not fall. The visible update
path does remove the nested client, its terminal parser, and one composition
hop; a direct launch has only sidebar plus provider panes.

The current environment could not validly measure first remote wheel paint,
queued-frame drain after a wheel burst, real Claude/Codex sustained output,
clipboard/mouse behavior through an actual terminal client, or long inline
Codex transcript resize over the same SSH link. Those measurements, plus a
confirmed macOS smoke run, remain gates for changing the default.

## Unresolved limitations

- tmux 2.7/2.8 cannot pre-size a home window with `resize-window`; switching
  uses native tmux reflow and may be visibly disruptive for a long inline TUI.
- A user who directly kills the *real display pane* still kills that provider;
  controlled Railmux close/preview/quit paths always return it home first.
- Synthetic output and switch timings are not evidence that Claude or Codex
  feels faster over SSH.
- The public dual-agent layout and its focus/border interaction remain separate
  roadmap work even though transport ownership is two-slot safe.
