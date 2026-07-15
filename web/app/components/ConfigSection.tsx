export default function ConfigSection() {
  return (
    <section
      id="config"
      className="bg-canvas border-t border-hairline py-24 sm:py-32"
    >
      <div className="max-w-7xl mx-auto px-8">
        <p className="text-sm font-[500] uppercase tracking-[0.35px] text-graphite mb-4">
          Configuration
        </p>
        <h2 className="text-[36px] sm:text-[40px] font-[400] leading-[1.0] tracking-[-0.9px] text-ink mb-8">
          Customize Railmux
        </h2>
        <p className="text-graphite text-base leading-relaxed max-w-2xl mb-10">
          Optional config file at{" "}
          <code className="text-ink bg-surface-cool px-1.5 py-0.5 rounded text-sm">
            ~/.config/railmux/config.toml
          </code>
          :
        </p>
        <div className="bg-scrim rounded-lg p-6 sm:p-8 overflow-x-auto max-w-2xl">
          <pre className="text-on-primary/80 text-sm leading-relaxed font-mono">
            <span className="text-on-primary/50"># ~/.config/railmux/config.toml</span>
            {"\n\n"}
            <span className="text-on-primary">[claude]</span>
            {"\n"}
            <span className="text-on-primary/70">binary = "claude"</span>
            {"\n\n"}
            <span className="text-on-primary">[codex]</span>
            {"\n"}
            <span className="text-on-primary/70">binary = "codex"</span>
            {"\n"}
            <span className="text-on-primary/70">home = "~/.codex"</span>
            {"\n\n"}
            <span className="text-on-primary">[projects]</span>
            {"\n"}
            <span className="text-on-primary/70">show_empty_projects = false</span>
            {"\n\n"}
            <span className="text-on-primary">[live]</span>
            {"\n"}
            <span className="text-on-primary/70">poll_interval_ms = 1000</span>
          </pre>
        </div>
      </div>
    </section>
  );
}
