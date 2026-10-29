/**
 * Access to the preload bridge. In a plain browser (renderer-only dev/E2E,
 * `bun dev:web`), `window.moru` is absent and a shim keeps the app usable:
 * engine coordinates come from ?port=&token= query params, folder picking
 * falls back to prompt().
 */

import type { DetectedInstance, MoruBridge } from "../../../shared/bridge";

function makeBrowserShim(): MoruBridge {
  const params = new URLSearchParams(window.location.search);
  const port = params.get("port") !== null ? Number(params.get("port")) : null;
  const token = params.get("token");
  const noop = (): void => undefined;
  return {
    platform: "linux",
    versions: { app: __APP_VERSION__, electron: "browser" },
    engine: {
      getInfo: async () => ({
        state: port !== null && token !== null ? "ready" : "failed",
        port,
        token,
        error: port === null ? "browser dev: pass ?port=&token=" : undefined,
        restarts: 0,
      }),
      onState: () => noop,
      restart: async () => window.location.reload(),
      logTail: async () => "",
    },
    pickFolder: async () => window.prompt("모드팩 폴더 경로 입력"),
    pickFolders: async () => {
      const value = window.prompt("모드팩 폴더 경로를 줄바꿈으로 구분해 입력");
      return value === null
        ? []
        : value
            .split(/\r?\n/)
            .map((path) => path.trim())
            .filter((path) => path.length > 0);
    },
    pickFile: async () => window.prompt("파일 경로 입력"),
    saveFile: async (defaultPath) => window.prompt("저장 경로 입력", defaultPath),
    probeModpack: async (path) => ({
      exists: path.length > 0,
      isDirectory: true,
      name: path.split(/[\\/]/).filter(Boolean).at(-1) ?? path,
      hasMods: true,
      hasConfig: true,
      hasKubejs: false,
      hasResourcepacks: false,
      modJarCount: 0,
    }),
    detectInstances: async (): Promise<DetectedInstance[]> => [],
    launcherRoots: async () => [],
    pathForFile: () => "",
    openPath: async () => undefined,
    showItemInFolder: async () => undefined,
    openExternal: async (url) => {
      window.open(url, "_blank");
    },
    win: {
      minimize: noop,
      toggleMaximize: noop,
      close: noop,
      isMaximized: async () => false,
      onMaximizeChange: () => noop,
    },
    secrets: {
      get: async (key) => window.localStorage.getItem(`secret:${key}`),
      set: async (key, value) => {
        window.localStorage.setItem(`secret:${key}`, value);
      },
      delete: async (key) => {
        window.localStorage.removeItem(`secret:${key}`);
      },
    },
    account: {
      // No loopback server in a plain browser: paste a token issued from
      // the web (or via the Electron app) to test logged-in states.
      login: async (webUrl) => {
        window.open(new URL("/auth/desktop-login?port=1", webUrl).toString(), "_blank");
        const token = window.prompt("moru.gg 데스크톱 API 토큰 입력");
        if (token === null || token.trim().length === 0) return null;
        const name = window.prompt("표시 이름", "dev") ?? "dev";
        return { token: token.trim(), name };
      },
      cancelLogin: noop,
    },
    // browser dev runs on an http origin — direct fetch, CORS willing
    webRequest: async (init) => {
      const res = await fetch(init.url, {
        method: init.method ?? "GET",
        headers: {
          ...(init.token !== undefined ? { authorization: `Bearer ${init.token}` } : {}),
          ...(init.body !== undefined ? { "content-type": "application/json" } : {}),
        },
        body: init.body,
      });
      return { status: res.status, body: await res.text() };
    },
    setBusy: noop,
    setTheme: noop,
    updates: {
      check: async () => undefined,
      install: noop,
      getState: async () => ({ status: "none" }),
      onState: () => noop,
    },
  };
}

export const moru: MoruBridge = window.moru ?? makeBrowserShim();
export const isElectron = window.moru !== undefined;
