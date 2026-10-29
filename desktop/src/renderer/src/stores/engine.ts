/** Sidecar handshake state, mirrored from the main process. */

import { create } from "zustand";

import type { EngineInfo } from "../../../shared/bridge";
import { moru } from "../lib/bridge";

interface EngineStore {
  info: EngineInfo;
}

export const useEngineStore = create<EngineStore>(() => ({
  info: { state: "starting", port: null, token: null, restarts: 0 },
}));

/**
 * Resolves true once the sidecar is ready (at once if it is), false if it
 * fails or `timeoutMs` passes first. A restart takes seconds; asking the
 * engine anything meanwhile only fails.
 */
export function waitForEngineReady(timeoutMs: number): Promise<boolean> {
  const state = useEngineStore.getState().info.state;
  if (state === "ready") return Promise.resolve(true);
  if (state === "failed") return Promise.resolve(false);
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      unsubscribe();
      resolve(false);
    }, timeoutMs);
    const unsubscribe = useEngineStore.subscribe(({ info }) => {
      if (info.state !== "ready" && info.state !== "failed") return;
      clearTimeout(timer);
      unsubscribe();
      resolve(info.state === "ready");
    });
  });
}

/** Port + token: changes exactly when a new engine process answers. */
export function engineGeneration(): string | null {
  const { port, token } = useEngineStore.getState().info;
  return port === null || token === null ? null : `${port}:${token}`;
}

export function bootEngineStore(): void {
  void moru.engine.getInfo().then((info) => useEngineStore.setState({ info }));
  moru.engine.onState((info) => useEngineStore.setState({ info }));
}
