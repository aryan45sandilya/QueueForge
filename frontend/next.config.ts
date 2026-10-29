import type { NextConfig } from "next";

// The browser talks to /api/* on the frontend origin; Next proxies it to the API so the
// session cookie stays first-party. QF_API_URL is read at build/start time on the server.
const apiUrl = process.env.QF_API_URL ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: "standalone",
  compress: false, // compression buffers Server-Sent Events through the proxy
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default config;
