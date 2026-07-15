import type { Route } from "./+types/home";
import NavBar from "~/components/NavBar";
import HeroSection from "~/components/HeroSection";
import FeaturesSection from "~/components/FeaturesSection";
import HowItWorksSection from "~/components/HowItWorksSection";
import KeybindingsSection from "~/components/KeybindingsSection";
import RoadmapSection from "~/components/RoadmapSection";
import ConfigSection from "~/components/ConfigSection";
import InstallRunSection from "~/components/InstallRunSection";
import FooterSection from "~/components/FooterSection";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "Railmux — Terminal UI for Claude Code and Codex sessions" },
    {
      name: "description",
      content:
        "Navigate, resume, and start Claude Code and Codex sessions across all your projects from one terminal UI. Built on tmux.",
    },
  ];
}

export default function Home() {
  return (
    <div className="min-h-screen bg-canvas text-ink font-sans">
      <NavBar />
      <HeroSection />
      <FeaturesSection />
      <HowItWorksSection />
      <KeybindingsSection />
      <RoadmapSection />
      <ConfigSection />
      <InstallRunSection />
      <FooterSection />
    </div>
  );
}
