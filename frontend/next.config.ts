import type { NextConfig } from "next";
import path from "path";
import { fileURLToPath } from "url";

const configDir = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  outputFileTracingRoot: configDir,
  async redirects() {
    return [
      { source: "/dashboard", destination: "/home", permanent: true },
      { source: "/apps", destination: "/home", permanent: true },
      { source: "/identity/create", destination: "/verify", permanent: true },
      { source: "/verify/aadhaar", destination: "/verify", permanent: true },
      { source: "/credentials", destination: "/activity", permanent: true },
      { source: "/verify/pan", destination: "/home", permanent: true },
    ];
  },
  webpack: (config) => {
    config.resolve.alias = {
      ...config.resolve.alias,
      "pino-pretty": false,
    };
    return config;
  },
};

export default nextConfig;
