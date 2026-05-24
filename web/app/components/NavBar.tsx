import { Link } from "react-router";

const NAV_LINKS = [
  { label: "Features", to: "#features" },
  { label: "How it works", to: "#how-it-works" },
  { label: "Roadmap", to: "#roadmap" },
  { label: "Config", to: "#config" },
];

export default function NavBar() {
  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-canvas/80 backdrop-blur-xl border-b border-hairline h-16">
      <div className="max-w-7xl mx-auto px-8 h-full flex items-center justify-between">
        <a href="#" className="text-ink text-lg font-[400]">
          ccmgr
        </a>
        <div className="hidden sm:flex items-center gap-8">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.label}
              to={link.to}
              className="text-ink-soft text-sm font-[600] leading-[1.43] hover:text-ink transition-colors"
            >
              {link.label}
            </Link>
          ))}
        </div>
        <Link
          to="#install"
          className="relative bg-ink text-on-primary text-sm font-semibold leading-[1.43] px-6 py-2 rounded-full overflow-hidden group"
        >
          <span className="relative z-10">Get Started</span>
          <span className="absolute inset-0 bg-gradient-to-r from-cyan-500/20 to-violet-500/20 opacity-0 group-hover:opacity-100 transition-opacity" />
        </Link>
      </div>
    </nav>
  );
}
