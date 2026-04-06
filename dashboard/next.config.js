/** @type {import('next').NextConfig} */
const nextConfig = {
  env: {
    MCP_API_URL: process.env.MCP_API_URL || 'http://localhost:8420',
  },
};

module.exports = nextConfig;
