import "@testing-library/jest-dom/vitest";

class TestResizeObserver implements ResizeObserver {
  static instances: TestResizeObserver[] = [];
  readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    TestResizeObserver.instances.push(this);
  }

  disconnect() {}
  observe() {}
  unobserve() {}
}

Object.defineProperty(globalThis, "ResizeObserver", {
  configurable: true,
  value: TestResizeObserver,
});
Object.defineProperty(HTMLElement.prototype, "clientWidth", {
  configurable: true,
  get: () => 1000,
});
Object.defineProperty(HTMLElement.prototype, "clientHeight", {
  configurable: true,
  get: () => 700,
});
Object.defineProperty(document, "hidden", {
  configurable: true,
  value: false,
});

window.requestAnimationFrame = (callback) =>
  window.setTimeout(() => callback(Date.now()), 0);
window.cancelAnimationFrame = (id) => window.clearTimeout(id);

export function triggerResizeObservers() {
  for (const observer of TestResizeObserver.instances) observer.callback([], observer);
}

export function resetResizeObservers() {
  TestResizeObserver.instances = [];
}
