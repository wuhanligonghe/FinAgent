import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://127.0.0.1:8001', browserName: 'chromium', channel: 'chromium', trace: 'retain-on-failure' },
  workers: 1,
  webServer: {
    command: 'PYTHONPATH=../src ../.venv/bin/python ../tests/serve_web_fixture.py',
    url: 'http://127.0.0.1:8001/api/v1/health',
    reuseExistingServer: false,
    timeout: 20000,
  },
});
