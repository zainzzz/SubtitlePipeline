/// <reference types="vitest" />
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Minimal config. Goals:
// - React component tests can use JSX + the standard testing-library matchers
// - Path resolution mirrors the existing vite.config.ts (no alias setup; the
//   app doesn't use them yet)
// - Watch mode is opt-in via `npm run test:watch`; `npm run test` is one-shot
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
})
