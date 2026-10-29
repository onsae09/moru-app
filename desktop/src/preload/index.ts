/**
 * Preload bridge: the renderer's only door to OS capabilities.
 * contextIsolation is on; everything crosses via contextBridge + ipcRenderer.
 */

import { contextBridge, ipcRenderer, webUtils } from "electron";

import type { DesktopPlatform, EngineInfo, MoruBridge, UpdateState } from "../shared/bridge";

function subscribe<T>(channel: string, cb: (payload: T) => void): () => void {
  const listener = (_event: Electron.IpcRendererEvent, payload: T): void => cb(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

// desktop builds only ship for these three platforms
const platform = process.platform as DesktopPlatform;

const bridge: MoruBridge = {
  platform,
  versions: {
    app: __APP_VERSION__,
    electron: process.versions.electron,
  },

  engine: {
    getInfo: () => ipcRenderer.invoke("engine:info"),
    onState: (cb: (info: EngineInfo) => void) => subscribe("engine:state", cb),
    restart: () => ipcRenderer.invoke("engine:restart"),
    logTail: () => ipcRenderer.invoke("engine:log-tail"),
  },

  pickFolder: () => ipcRenderer.invoke("dialog:pick-folder"),
  pickFolders: () => ipcRenderer.invoke("dialog:pick-folders"),
  pickFile: (filters) => ipcRenderer.invoke("dialog:pick-file", filters),
  saveFile: (defaultPath) => ipcRenderer.invoke("dialog:save-file", defaultPath),
  probeModpack: (path) => ipcRenderer.invoke("fs:probe-modpack", path),
  detectInstances: (paths) => ipcRenderer.invoke("fs:detect-instances", paths),
  launcherRoots: () => ipcRenderer.invoke("fs:launcher-roots"),
  pathForFile: (file) => webUtils.getPathForFile(file),

  openPath: (path) => ipcRenderer.invoke("shell:open-path", path),
  showItemInFolder: (path) => ipcRenderer.invoke("shell:show-item", path),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url),

  win: {
    minimize: () => ipcRenderer.send("win:minimize"),
    toggleMaximize: () => ipcRenderer.send("win:toggle-maximize"),
    close: () => ipcRenderer.send("win:close"),
    isMaximized: () => ipcRenderer.invoke("win:is-maximized"),
    onMaximizeChange: (cb: (maximized: boolean) => void) => subscribe("win:maximize-change", cb),
  },

  secrets: {
    get: (key) => ipcRenderer.invoke("secrets:get", key),
    set: (key, value) => ipcRenderer.invoke("secrets:set", key, value),
    delete: (key) => ipcRenderer.invoke("secrets:delete", key),
  },

  account: {
    login: (webUrl) => ipcRenderer.invoke("account:login", webUrl),
    cancelLogin: () => ipcRenderer.send("account:login-cancel"),
  },

  webRequest: (init) => ipcRenderer.invoke("web:request", init),

  setBusy: (value) => ipcRenderer.send("app:set-busy", value),

  updates: {
    check: () => ipcRenderer.invoke("updates:check"),
    install: () => ipcRenderer.send("updates:install"),
    getState: () => ipcRenderer.invoke("updates:get-state"),
    onState: (cb: (state: UpdateState) => void) => subscribe("updates:state", cb),
  },
};

contextBridge.exposeInMainWorld("moru", bridge);
