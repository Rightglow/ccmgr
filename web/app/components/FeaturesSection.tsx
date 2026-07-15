const FEATURES = [
  {
    title: "Seamless switching",
    description:
      "Flip between Claude Code and Codex sessions with a single keystroke. No more losing context — each session stays alive in the background.",
  },
  {
    title: "Persistent sessions",
    description:
      "Every agent runs in its own detached tmux session. Responses and tool calls keep running even when you switch away.",
  },
  {
    title: "Project-aware TUI",
    description:
      "Railmux reads each agent's project history. Browse sessions by project, filter by name, and resume exactly where you left off.",
  },
];

export default function FeaturesSection() {
  return (
    <section id="features" className="bg-canvas py-24 sm:py-32">
      <div className="max-w-7xl mx-auto px-8">
        <p className="text-sm font-[500] uppercase tracking-[0.35px] text-graphite mb-4">
          Features
        </p>
        <h2 className="text-[36px] sm:text-[40px] font-[400] leading-[1.0] tracking-[-0.9px] text-ink mb-16">
          Why Railmux?
        </h2>
        <div className="grid md:grid-cols-3 gap-x-12 gap-y-12">
          {FEATURES.map((f) => (
            <div key={f.title}>
              <div className="w-10 h-10 rounded-full bg-primary mb-6 flex items-center justify-center">
                <div className="w-4 h-4 rounded-full bg-on-primary" />
              </div>
              <h3 className="text-ink text-[24px] font-[400] leading-[1.0] mb-3">
                {f.title}
              </h3>
              <p className="text-graphite text-base leading-relaxed">
                {f.description}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
