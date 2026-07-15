export default function FooterSection() {
  return (
    <footer className="bg-footer py-16">
      <div className="max-w-7xl mx-auto px-8">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-6">
          <div>
            <a href="#" className="text-on-primary text-lg font-[400]">
              Railmux
            </a>
            <p className="text-on-primary/40 text-sm mt-2">
              Claude Code and Codex session manager
            </p>
          </div>
          <div className="flex items-center gap-6">
            <a
              href="https://github.com/Rightglow/Railmux"
              className="text-on-primary/60 text-sm hover:text-on-primary transition-colors"
            >
              GitHub
            </a>
            <a
              href="https://pypi.org/project/railmux/"
              className="text-on-primary/60 text-sm hover:text-on-primary transition-colors"
            >
              PyPI
            </a>
          </div>
        </div>
        <div className="border-t border-on-primary/10 mt-10 pt-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <p className="text-on-primary/40 text-[13px]">
            &copy; {new Date().getFullYear()} Railmux contributors. MIT License.
          </p>
        </div>
      </div>
    </footer>
  );
}
