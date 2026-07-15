export default function HowItWorksSection() {
  return (
    <section
      id="how-it-works"
      className="bg-canvas border-t border-hairline py-24 sm:py-32"
    >
      <div className="max-w-7xl mx-auto px-8">
        <p className="text-sm font-[500] uppercase tracking-[0.35px] text-graphite mb-4">
          How it works
        </p>
        <h2 className="text-[36px] sm:text-[40px] font-[400] leading-[1.0] tracking-[-0.9px] text-ink mb-8">
          Built on tmux
        </h2>
        <div className="space-y-6 max-w-3xl mb-12">
          <p className="text-graphite text-base leading-relaxed">
            Railmux reads{" "}
            <code className="text-ink bg-surface-cool px-1.5 py-0.5 rounded text-sm">
              ~/.claude/projects/*
            </code>{" "}
            and lists every session. Pressing Enter on a session does
            two things: (1) if a detached tmux session named{" "}
            <code className="text-ink bg-surface-cool px-1.5 py-0.5 rounded text-sm">
              cc-{`<short_id>`}
            </code>{" "}
            running{" "}
            <code className="text-ink bg-surface-cool px-1.5 py-0.5 rounded text-sm">
              claude --resume {`<id>`}
            </code>{" "}
            doesn't already exist, it creates one with{" "}
            <code className="text-ink bg-surface-cool px-1.5 py-0.5 rounded text-sm">
              tmux new-session -d
            </code>
            ; (2) the right pane runs{" "}
            <code className="text-ink bg-surface-cool px-1.5 py-0.5 rounded text-sm">
              tmux attach -t cc-{`<short_id>`}
            </code>{" "}
            so you see and interact with that claude.
          </p>
          <p className="text-graphite text-base leading-relaxed">
            Switching sessions just respawns the right pane to attach
            to a different background tmux session — the detached
            claudes keep running with all their state intact.
            Keystrokes in the right pane go directly to claude: full
            color, full TUI.
          </p>
        </div>
        <div className="bg-scrim rounded-lg p-6 sm:p-8 overflow-x-auto">
          <pre className="text-on-primary/80 text-sm leading-relaxed font-mono">
            <span className="text-on-primary/50"># Railmux sits in the left pane</span>
            {"\n"}
            <span className="text-on-primary">$ railmux</span>
            {"\n\n"}
            <span className="text-on-primary/50"># Each claude runs as a detached tmux session</span>
            {"\n"}
            <span className="text-on-primary/50"># The right pane shows the active one</span>
            {"\n"}
            <span className="text-on-primary">$ tmux ls</span>
            {"\n"}
            <span className="text-on-primary/70">cc-abc12345: 1 windows (created ...)</span>
            {"\n"}
            <span className="text-on-primary/70">cc-def67890: 1 windows (created ...)</span>
            {"\n\n"}
            <span className="text-on-primary/50"># Switching re-attaches the right pane</span>
            {"\n"}
            <span className="text-on-primary/50"># Previous claude keeps running, detached</span>
          </pre>
        </div>
      </div>
    </section>
  );
}
