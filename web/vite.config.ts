import { defineConfig } from "vite";
export default defineConfig({
  server: {
    proxy: {
      "/admin/api": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000",
    },
  },
});
