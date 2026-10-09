import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run build` writes dist/, which dashboard.py serves at http://127.0.0.1:8050.
// `npm run dev` serves the page with live reload and forwards data requests to a running dashboard.py.
const BACKEND = "http://127.0.0.1:8050";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": BACKEND, "/img": BACKEND },
  },
});
