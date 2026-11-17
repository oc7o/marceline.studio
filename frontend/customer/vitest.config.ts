/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@shared": path.resolve(import.meta.dirname, "../src") },
  },
  test: {
    include: ["{app,components,screens,lib}/**/*.test.{ts,tsx}"],
    environment: "node",
  },
});
