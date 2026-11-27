import { defineConfig } from "vitest/config";
import { localClipsUpdatePlugin } from "./vite-plugin-local-clips";

export default defineConfig({
  base: "./",
  plugins: [localClipsUpdatePlugin()],
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  test: {
    environment: "node",
    include: ["src/tests/**/*.test.ts"],
  },
});
