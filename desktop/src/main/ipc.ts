/**
 * IPC surface backing the `window.moru` preload bridge.
 * All OS capabilities the renderer may touch live here.
 */

import { existsSync, readdirSync, statSync } from "node:fs";
import http from "node:http";
import path from "node:path";

import { BrowserWindow, app, dialog, ipcMain, safeStorage, shell } from "electron";

import type { ModpackProbe, WebAccount, WebRequestInit, WebResponse } from "../shared/bridge";
import { detectInstances, launcherRoots } from "./launchers";
import { SecretStore } from "./secretStore";
import type { EngineSidecar } from "./sidecar";

/* ---- secrets (API keys) ---- */

let secretStore: SecretStore | null = null;
const secrets = (): SecretStore =>
  (secretStore ??= new SecretStore(path.join(app.getPath("userData"), "secrets.json")));

async function readSecrets(): Promise<Record<string, string>> {
  try {
    return await secrets().read();
  } catch (error) {
    // Moved aside by the store; reads see no key until one is saved again.
    console.error(`[secrets] ${String(error)}`);
    return {};
  }
}

/* ---- desktop login ----
   "로그인" opens the default browser at {webUrl}/auth/desktop-login?port=N;
   after Discord OAuth the web redirects to http://127.0.0.1:N/callback with
   the token. One pending attempt at a time; the token is never logged. */

const LOGIN_TIMEOUT_MS = 5 * 60_000;

const CALLBACK_HTML = `<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>moru</title></head>
<body style='margin:0;display:flex;align-items:center;justify-content:center;height:100vh;background:#0E1512;color:#EAF5EF;font-family:Pretendard, "Malgun Gothic", "Apple SD Gothic Neo", sans-serif'>
<div style="text-align:center"><div style="font-size:18px;font-weight:700;margin-bottom:8px">로그인 완료</div>
<div style="font-size:13px;color:#A2B3AB">moru 앱으로 돌아가세요. 이 창은 닫아도 됩니다.</div></div>
</body></html>`;

let pendingLogin: {
  server: http.Server;
  resolve: (account: WebAccount | null) => void;
} | null = null;

function settleLogin(result: WebAccount | null): void {
  if (pendingLogin === null) return;
  const { server, resolve } = pendingLogin;
  pendingLogin = null;
  server.close();
  resolve(result);
}

function startLogin(webUrl: string): Promise<WebAccount | null> {
  settleLogin(null); // supersede a stale attempt
  const { promise, resolve } = Promise.withResolvers<WebAccount | null>();

  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    if (url.pathname !== "/callback") {
      res.statusCode = 404;
      res.end();
      return;
    }
    const token = url.searchParams.get("token");
    const name = url.searchParams.get("name") ?? "";
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(CALLBACK_HTML);
    settleLogin(token !== null && token.length > 0 ? { token, name } : null);
  });

  server.on("error", () => settleLogin(null));
  server.listen(0, "127.0.0.1", () => {
    const addr = server.address();
    if (addr === null || typeof addr === "string") {
      server.close();
      resolve(null);
      return;
    }
    const target = new URL("/auth/desktop-login", webUrl);
    target.searchParams.set("port", String(addr.port));
    void shell.openExternal(target.toString());
    const timer = setTimeout(() => {
      if (pendingLogin?.server === server) settleLogin(null);
    }, LOGIN_TIMEOUT_MS);
    timer.unref();
  });

  pendingLogin = { server, resolve };
  return promise;
}

/* ---- modpack probing ---- */

function probeModpack(target: string): ModpackProbe {
  const empty: ModpackProbe = {
    exists: false,
    isDirectory: false,
    name: path.basename(target),
    hasMods: false,
    hasConfig: false,
    hasKubejs: false,
    hasResourcepacks: false,
    modJarCount: 0,
  };
  let stats;
  try {
    stats = statSync(target);
  } catch {
    return empty;
  }
  if (!stats.isDirectory()) return { ...empty, exists: true };
  const has = (dir: string): boolean => existsSync(path.join(target, dir));
  let modJarCount = 0;
  if (has("mods")) {
    try {
      modJarCount = readdirSync(path.join(target, "mods")).filter((f) =>
        f.endsWith(".jar"),
      ).length;
    } catch {
      modJarCount = 0;
    }
  }
  return {
    exists: true,
    isDirectory: true,
    name: path.basename(target),
    hasMods: has("mods"),
    hasConfig: has("config"),
    hasKubejs: has("kubejs"),
    hasResourcepacks: has("resourcepacks"),
    modJarCount,
  };
}

/* ---- registration ---- */

export function registerIpc(sidecar: EngineSidecar, getBusy: () => boolean): void {
  void getBusy; // busy state is read by the quit flow in index.ts

  ipcMain.handle("engine:info", () => sidecar.info);
  ipcMain.handle("engine:restart", () => sidecar.restart());
  ipcMain.handle("engine:log-tail", () => sidecar.logTail);

  ipcMain.handle("dialog:pick-folder", async (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win === null) return null;
    const result = await dialog.showOpenDialog(win, { properties: ["openDirectory"] });
    return result.canceled ? null : result.filePaths[0];
  });

  ipcMain.handle("dialog:pick-folders", async (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win === null) return [];
    const result = await dialog.showOpenDialog(win, {
      properties: ["openDirectory", "multiSelections"],
    });
    return result.canceled ? [] : result.filePaths;
  });

  ipcMain.handle(
    "dialog:pick-file",
    async (event, filters?: { name: string; extensions: string[] }[]) => {
      const win = BrowserWindow.fromWebContents(event.sender);
      if (win === null) return null;
      const result = await dialog.showOpenDialog(win, { properties: ["openFile"], filters });
      return result.canceled ? null : result.filePaths[0];
    },
  );

  ipcMain.handle("dialog:save-file", async (event, defaultPath?: string) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win === null) return null;
    const result = await dialog.showSaveDialog(win, { defaultPath });
    return result.canceled ? null : result.filePath;
  });

  ipcMain.handle("fs:probe-modpack", (_event, target: string) => probeModpack(target));
  // The renderer passes its persisted per-launcher folders on every call, so
  // a folder changed in Settings is scanned without a restart.
  ipcMain.handle("fs:detect-instances", (_event, paths: unknown) => detectInstances(paths));
  ipcMain.handle("fs:launcher-roots", () => launcherRoots());

  // Folders only: opening an arbitrary file would also launch executables.
  ipcMain.handle("shell:open-path", async (_event, target: string) => {
    let isDirectory = false;
    try {
      isDirectory = statSync(target).isDirectory();
    } catch {
      isDirectory = false;
    }
    if (!isDirectory) throw new Error(`refusing to open a non-folder path: ${target}`);
    await shell.openPath(target);
  });
  ipcMain.handle("shell:show-item", (_event, target: string) => {
    shell.showItemInFolder(target);
  });
  ipcMain.handle("shell:open-external", async (_event, url: string) => {
    if (!/^https?:\/\//.test(url)) throw new Error(`refusing to open non-http url: ${url}`);
    await shell.openExternal(url);
  });

  /* window controls (frameless titlebar) */
  ipcMain.on("win:minimize", (event) => {
    BrowserWindow.fromWebContents(event.sender)?.minimize();
  });
  ipcMain.on("win:toggle-maximize", (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win === null) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });
  ipcMain.on("win:close", (event) => {
    BrowserWindow.fromWebContents(event.sender)?.close();
  });
  ipcMain.handle("win:is-maximized", (event) => {
    return BrowserWindow.fromWebContents(event.sender)?.isMaximized() ?? false;
  });

  /* secrets via safeStorage; falls back to plaintext only when the OS
     keychain is unavailable (headless dev) - values are still local-only. */
  ipcMain.handle("secrets:get", async (_event, key: string) => {
    const store = await readSecrets();
    const value = store[key];
    if (value === undefined) return null;
    if (!value.startsWith("enc:")) return value;
    if (!safeStorage.isEncryptionAvailable()) return null;
    try {
      return safeStorage.decryptString(Buffer.from(value.slice(4), "base64"));
    } catch {
      return null;
    }
  });
  ipcMain.handle("secrets:set", async (_event, key: string, value: string) => {
    const stored = safeStorage.isEncryptionAvailable()
      ? `enc:${safeStorage.encryptString(value).toString("base64")}`
      : value;
    await secrets().update((store) => {
      store[key] = stored;
    });
  });
  ipcMain.handle("secrets:delete", async (_event, key: string) => {
    await secrets().update((store) => {
      delete store[key];
    });
  });

  /* web API proxy — renderer pages load from file:// where CORS blocks
     direct fetches to moru.gg/openrouter; main-process fetch is exempt. */
  ipcMain.handle("web:request", async (_event, init: WebRequestInit): Promise<WebResponse> => {
    const url = new URL(init.url);
    if (url.protocol !== "https:" && url.hostname !== "localhost" && url.hostname !== "127.0.0.1") {
      throw new Error(`refusing non-https web request: ${init.url}`);
    }
    const res = await fetch(url, {
      method: init.method ?? "GET",
      headers: {
        ...(init.token !== undefined ? { authorization: `Bearer ${init.token}` } : {}),
        ...(init.body !== undefined ? { "content-type": "application/json" } : {}),
      },
      body: init.body,
      signal: AbortSignal.timeout(30_000),
    });
    return { status: res.status, body: await res.text() };
  });

  /* desktop login */
  ipcMain.handle("account:login", (_event, webUrl: string) => {
    if (!/^https?:\/\//.test(webUrl)) {
      throw new Error(`refusing to open non-http login url: ${webUrl}`);
    }
    return startLogin(webUrl);
  });
  ipcMain.on("account:login-cancel", () => settleLogin(null));
}
