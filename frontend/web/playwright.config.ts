import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 60_000,
  use: {
    baseURL: 'http://127.0.0.1:8765',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
    launchOptions: process.env.OPENHARNESS_TEST_BROWSER ? { executablePath: process.env.OPENHARNESS_TEST_BROWSER } : {},
  },
  webServer: {
    command: '../../.venv/bin/python ../../tests/test_web/browser_server.py',
    url: 'http://127.0.0.1:8765/api/health',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
