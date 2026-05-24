const BINDINGS = [
  { key: "\u2191 / \u2193", action: "Move selection within the focused pane" },
  { key: "Tab / Shift+Tab", action: "Cycle focus through panes (focused pane gets bright cyan border)" },
  { key: "Enter", action: "Resume or start the selected session — claude opens in the right pane" },
  { key: "Ctrl+B then \u2190", action: "Move focus back to ccmgr from the right pane" },
  { key: "Ctrl+B then \u2192", action: "Move focus to the claude pane" },
  { key: "/", action: "Filter the focused pane" },
  { key: "i", action: "Popup with the focused session's details (title, project, msgs, tokens)" },
  { key: "?", action: "Full help popup" },
  { key: "q / Ctrl+C", action: "Quit (kills right tmux pane + auto-launched tmux session)" },
  { key: "c", action: "Open focused project in VS Code" },
  { key: "t", action: "Open terminal in focused project" },
];

export default function KeybindingsSection() {
  return (
    <section className="bg-canvas border-t border-hairline py-24 sm:py-32">
      <div className="max-w-7xl mx-auto px-8">
        <p className="text-sm font-[500] uppercase tracking-[0.35px] text-graphite mb-4">
          Keyboard
        </p>
        <h2 className="text-[36px] sm:text-[40px] font-[400] leading-[1.0] tracking-[-0.9px] text-ink mb-16">
          Key bindings
        </h2>
        <div className="max-w-3xl">
          {BINDINGS.map((b) => (
            <div
              key={b.key}
              className="grid grid-cols-[auto_1fr] gap-x-8 gap-y-0 py-3 border-b border-hairline last:border-0 items-baseline"
            >
              <code className="text-ink text-sm font-mono font-[500] whitespace-nowrap">
                {b.key}
              </code>
              <span className="text-graphite text-base leading-relaxed">
                {b.action}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
