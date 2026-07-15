export default function InstallRunSection() {
  return (
    <section
      id="install"
      className="bg-scrim py-24 sm:py-32"
    >
      <div className="max-w-7xl mx-auto px-8">
        <div className="max-w-3xl mx-auto">
          <h2 className="text-on-primary text-[36px] sm:text-[40px] font-[400] leading-[1.0] tracking-[-0.9px] mb-12 text-center">
            Get started
          </h2>

          <div className="space-y-8 mb-12">
            <div>
              <p className="text-on-primary/60 text-sm font-[500] uppercase tracking-[0.35px] mb-3">
                Install
              </p>
              <p className="text-on-primary/70 text-base leading-relaxed mb-4">
                Requires Python 3.9+ and tmux on PATH.
              </p>
              <div className="inline-flex items-center gap-3 bg-black/40 rounded-full px-6 py-3 sm:px-8 sm:py-4 w-full sm:w-auto">
                <span className="text-on-primary/50 text-sm font-mono">$</span>
                <code className="text-on-primary text-base sm:text-lg font-mono flex-1">
                  pip install railmux
                </code>
                <button
                  onClick={() => navigator.clipboard.writeText("pip install railmux")}
                  className="text-on-primary/50 hover:text-on-primary transition-colors text-sm font-mono ml-2 shrink-0"
                  aria-label="Copy to clipboard"
                >
                  &#x2398;
                </button>
              </div>
            </div>
          </div>

          <div className="text-center">
            <a
              href="https://github.com/Rightglow/Railmux"
              className="bg-on-primary text-primary text-sm font-semibold leading-[1.43] px-8 py-3 rounded-full hover:opacity-90 transition-opacity inline-block"
            >
              View on GitHub
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
