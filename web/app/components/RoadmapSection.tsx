const ITEMS = [
  "Cross-session search across projects",
  "Cost and token-usage dashboard",
  "Mouse support",
  "Renaming and closing individual sessions from within ccmgr",
];

export default function RoadmapSection() {
  return (
    <section
      id="roadmap"
      className="bg-canvas border-t border-hairline py-24 sm:py-32"
    >
      <div className="max-w-7xl mx-auto px-8">
        <p className="text-sm font-[500] uppercase tracking-[0.35px] text-graphite mb-4">
          Roadmap
        </p>
        <h2 className="text-[36px] sm:text-[40px] font-[400] leading-[1.0] tracking-[-0.9px] text-ink mb-8">
          What's next
        </h2>
        <ul className="space-y-4 max-w-xl mb-10">
          {ITEMS.map((item) => (
            <li key={item} className="flex items-start gap-3 text-graphite text-base leading-relaxed">
              <span className="text-ink mt-1.5 shrink-0 w-1.5 h-1.5 rounded-full bg-ink" />
              {item}
            </li>
          ))}
        </ul>
        <p className="text-graphite text-base leading-relaxed">
          Issues and pull requests welcome on{" "}
          <a
            href="https://github.com/regmi-saugat/ccmgr"
            className="text-ink underline underline-offset-2 decoration-hairline hover:decoration-ink transition-colors"
          >
            GitHub
          </a>
          .
        </p>
      </div>
    </section>
  );
}
