import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.js",
    include: ["src/**/*.test.{js,jsx}"],
  },
  server: {
    port: 5173,
    host: "0.0.0.0",
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
