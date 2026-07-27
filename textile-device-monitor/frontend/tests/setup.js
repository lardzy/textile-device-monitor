import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { server } from './testServer';

const originalConsoleError = console.error;

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' });
  vi.spyOn(console, 'error').mockImplementation((...args) => {
    if (
      typeof args[0] === 'string'
      && args[0].includes('not wrapped in act')
    ) {
      return;
    }
    originalConsoleError(...args);
  });
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => {
  server.close();
  console.error.mockRestore?.();
});

window.matchMedia = window.matchMedia || (() => ({
  matches: false,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
}));

window.ResizeObserver = window.ResizeObserver || class ResizeObserver {
  observe() {}

  unobserve() {}

  disconnect() {}
};

window.HTMLElement.prototype.scrollIntoView = () => {};

const jsdomGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = element => jsdomGetComputedStyle(element);
