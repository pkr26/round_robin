import type { NextConfig } from 'next';
import path from 'node:path';

const config: NextConfig = {
  output: 'export',
  poweredByHeader: false,
  webpack(config) {
    if (process.env.ROUND_ROBIN_SOURCE_COVERAGE === '1') {
      config.module.rules.unshift({ test: /\.tsx?$/, include: path.join(process.cwd(), 'app'), enforce: 'pre', use: [path.join(process.cwd(), '../scripts/frontend_istanbul_loader.cjs')] });
    }
    return config;
  },
  productionBrowserSourceMaps: process.env.ROUND_ROBIN_AUDIT_SOURCE_MAPS === '1',
};
export default config;
