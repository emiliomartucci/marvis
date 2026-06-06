import { defineConfig, devices } from '@playwright/test';
import fs from 'fs';
import path from 'path';

// Load local Playwright secrets (not committed — see .gitignore)
const envFile = path.resolve(__dirname, '.env.playwright');
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, 'utf-8').split('\n')) {
    const match = line.match(/^([^#=]+)=(.*)$/);
    if (match) process.env[match[1].trim()] ??= match[2].trim();
  }
}

export default defineConfig({
  testDir: './playwright/tests',
  timeout: 30_000,
  retries: 0,
  reporter: 'line',
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:3000',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  globalSetup: './playwright/global-setup.ts',
  outputDir: './playwright/results',
});
