/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  env: {
    MCP_API_URL: process.env.NEXT_PUBLIC_MCP_API_URL || 'http://localhost:8420',
  },
};

module.exports = nextConfig;
