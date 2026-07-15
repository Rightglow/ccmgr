import { Link } from "react-router";

export default function HeroSection() {
  return (
    <section className="bg-scrim min-h-[90vh] flex items-center relative overflow-hidden">
      <div className="absolute top-[-10%] left-[-10%] w-[40rem] h-[40rem] bg-cyan-500/10 rounded-full blur-[120px] animate-pulse-slow mix-blend-screen pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[-10%] w-[40rem] h-[40rem] bg-violet-500/10 rounded-full blur-[120px] animate-pulse-slow mix-blend-screen pointer-events-none" style={{ animationDelay: "2s" }} />
      <div className="absolute top-[40%] left-[60%] w-[30rem] h-[30rem] bg-fuchsia-500/10 rounded-full blur-[100px] animate-pulse-slow mix-blend-screen pointer-events-none" style={{ animationDelay: "4s" }} />
      <div className="absolute inset-0 opacity-[0.05] bg-[image:repeating-linear-gradient(0deg,transparent,transparent_48px,white_48px,white_49px),repeating-linear-gradient(90deg,transparent,transparent_48px,white_48px,white_49px)] [mask-image:radial-gradient(ellipse_at_center,black_40%,transparent_80%)] pointer-events-none" />
      <div className="max-w-7xl mx-auto px-8 py-32 relative z-10 w-full">
        <div className="max-w-3xl">
          <div className="inline-block relative mb-8">
            <div className="absolute inset-0 bg-gradient-to-r from-cyan-500/40 to-violet-500/40 blur-xl opacity-50" />
          </div>
          <h1 className="text-transparent bg-clip-text bg-gradient-to-r from-white via-zinc-200 to-zinc-400 bg-[length:200%_auto] text-[56px] sm:text-[72px] lg:text-[88px] font-[500] leading-[1.0] tracking-[-2px] mb-8 drop-shadow-sm">
            Railmux
          </h1>
          <p className="text-on-primary/70 text-lg sm:text-xl leading-relaxed max-w-2xl mb-12 font-[300]">
            A terminal UI to navigate, resume, and start Claude Code and Codex
            sessions across all your projects from one place. Every
            session runs in its own detached tmux session —
            switching preserves all in-progress work.
          </p>
          <div className="flex flex-wrap gap-5">
            <Link
              to="#install"
              className="group relative inline-flex items-center justify-center bg-on-primary text-ink text-sm font-semibold leading-[1.43] px-8 py-3.5 rounded-full transition-all hover:scale-105 hover:shadow-[0_0_20px_rgba(255,255,255,0.3)]"
            >
              Get Started
            </Link>
            <Link
              to="#how-it-works"
              className="bg-transparent text-on-primary text-sm font-semibold leading-[1.43] px-8 py-3.5 rounded-full border border-on-primary/20 hover:bg-on-primary/10 hover:border-on-primary/50 backdrop-blur-sm transition-all"
            >
              How it works
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}
