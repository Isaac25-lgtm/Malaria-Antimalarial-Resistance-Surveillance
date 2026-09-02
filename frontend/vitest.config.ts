import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Vitest configuration.
 *
 * Kept separate from vite.config.ts because Vitest bundles its own copy of
 * Vite; sharing one config file makes the two Plugin types structurally
 * incompatible under strict TypeScript.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
  },
});
