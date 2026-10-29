import { EventEmitter } from "node:events";

import { expect, mock, test } from "bun:test";

/** Children that die right after spawning (a missing DLL, a bad build). */
const spawned = [];
mock.module("node:child_process", () => ({
  spawn: mock(() => {
    const child = Object.assign(new EventEmitter(), {
      exitCode: null,
      stdout: new EventEmitter(),
      stderr: new EventEmitter(),
      stdin: Object.assign(new EventEmitter(), { end() {} }),
      kill() {
        if (this.exitCode !== null) return;
        this.exitCode = 1;
        queueMicrotask(() => this.emit("exit", 1));
      },
    });
    spawned.push(child);
    setTimeout(() => child.kill(), 5);
    return child;
  }),
}));
// mock.module is process-wide in bun: keep the shape other test files use.
mock.module("electron", () => ({
  app: { isPackaged: false, getAppPath: () => "/tmp/app", getPath: () => "/tmp/moru-sidecar-test" },
  BrowserWindow: { getAllWindows: () => [] },
  ipcMain: { handle: () => undefined, on: () => undefined },
}));
globalThis.fetch = async () => {
  throw new Error("connection refused");
};

const { EngineSidecar } = await import("./sidecar.ts?crash-loop-test");

test("a child that dies at startup costs one restart, never two in parallel", async () => {
  const sidecar = new EngineSidecar();
  const states = [];
  sidecar.on("state", (info) => states.push(info.state));
  await sidecar.start();
  // Wait out the restart chain (3 × 500 ms delay plus fast-failing health).
  const deadline = Date.now() + 8_000;
  while (sidecar.info.state !== "failed" && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 50));
  }
  expect(sidecar.info.state).toBe("failed");
  // First spawn + MAX_RESTARTS (3) restarts — not double that.
  expect(spawned.length).toBe(4);
  expect(sidecar.info.restarts).toBe(3);
}, 10_000);

test("restart() from the failed state starts over with a full budget", async () => {
  const sidecar = new EngineSidecar();
  await sidecar.start();
  const deadline = Date.now() + 8_000;
  while (sidecar.info.state !== "failed" && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 50));
  }
  const before = spawned.length;
  void sidecar.restart();
  await new Promise((r) => setTimeout(r, 30));
  expect(spawned.length).toBe(before + 1);
  expect(sidecar.info.restarts).toBe(0);
  await sidecar.stop();
}, 10_000);
