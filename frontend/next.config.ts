import type { NextConfig } from "next";

const sandboxWorkerThreads = process.env.BIDREADY_SANDBOX_WORKER_THREADS === "1";

// BIDREADY_STATIC_EXPORT=1 emits plain HTML/JS into ./out, which FastAPI can
// serve directly. Everything this app fetches is client-side, so there is
// nothing for a Node server to render.
const staticExport = process.env.BIDREADY_STATIC_EXPORT === "1";

const nextConfig: NextConfig = {
  output: staticExport ? "export" : "standalone",
  ...(staticExport ? { trailingSlash: true, images: { unoptimized: true } } : {}),
  // `pnpm typecheck` is the explicit CI gate. Avoid running the same check in
  // a separate child process during the container build.
  typescript: { ignoreBuildErrors: true },
  experimental: sandboxWorkerThreads
    ? { cpus: 1, workerThreads: true, webpackBuildWorker: false }
    : {},
};

export default nextConfig;
