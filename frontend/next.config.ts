import type { NextConfig } from "next";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  // Monorepo: evita que o Turbopack use um package-lock.json acima de `frontend/`.
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
