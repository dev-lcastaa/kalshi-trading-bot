import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

class MockWebSocket {
  constructor() {
    window.setTimeout(() => this.onopen?.(), 0);
  }

  close() {}
}

globalThis.WebSocket = MockWebSocket;
globalThis.requestAnimationFrame = (callback) => window.setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (handle) => window.clearTimeout(handle);
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: { writeText: vi.fn().mockResolvedValue(undefined) },
});
