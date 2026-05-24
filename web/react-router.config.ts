import type { Config } from "@react-router/dev/config";

export default {
  ssr: false,
  basename: "/ccmgr/",
  prerender: ["/"],
} satisfies Config;
