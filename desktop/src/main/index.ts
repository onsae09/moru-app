/**
 * Electron main process: frameless window, engine sidecar lifecycle,
 * quit confirmation while a job is running.
 */

import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { BrowserWindow, app, dialog, ipcMain, shell } from "electron";

import { registerIpc } from "./ipc";
import { EngineSidecar } from "./sidecar";
import { registerUpdater } from "./updater";

const sidecar = new EngineSidecar();

/** Window background per theme — the dark and light `--color-bg` tokens. */
const WINDOW_BACKGROUND = { dark: "#0E1512", light: "#F6F9F7" } as const;
type WindowTheme = keyof typeof WINDOW_BACKGROUND;

const themeFile = (): string => path.join(app.getPath("userData"), "window-theme.json");

/** The theme the renderer last reported, so the first paint already matches. */
function savedTheme(): WindowTheme {
  try {
    const value: unknown = JSON.parse(readFileSync(themeFile(), "utf-8"));
    return value === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}
let busy = false;
let quitConfirmed = false;
let shutdownStarted = false;

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1200,
    minHeight: 760,
    frame: false,
    show: false,
    backgroundColor: WINDOW_BACKGROUND[savedTheme()],
    webPreferences: {
      preload: path.join(import.meta.dirname, "../preload/index.mjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.on("ready-to-show", () => win.show());
  win.on("maximize", () => win.webContents.send("win:maximize-change", true));
  win.on("unmaximize", () => win.webContents.send("win:maximize-change", false));

  // external links always go to the OS browser
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });

  // The app never navigates its own window; a link that tries to would
  // replace the UI (and its preload bridge) with a remote page.
  win.webContents.on("will-navigate", (event, url) => {
    if (url !== win.webContents.getURL()) {
      event.preventDefault();
      if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    }
  });

  if (process.env.ELECTRON_RENDERER_URL !== undefined) {
    void win.loadURL(process.env.ELECTRON_RENDERER_URL);
  } else {
    void win.loadFile(path.join(import.meta.dirname, "../renderer/index.html"));
  }
  return win;
}

void app.whenReady().then(() => {
  registerIpc(sidecar, () => busy);
  registerUpdater();

  ipcMain.on("app:set-busy", (_event, value: boolean) => {
    busy = value;
  });
  ipcMain.on("app:set-theme", (event, value: unknown) => {
    const theme: WindowTheme = value === "light" ? "light" : "dark";
    BrowserWindow.fromWebContents(event.sender)?.setBackgroundColor(WINDOW_BACKGROUND[theme]);
    if (theme !== savedTheme()) {
      try {
        writeFileSync(themeFile(), JSON.stringify(theme), "utf-8");
      } catch {
        // cosmetic only: the next launch paints dark first
      }
    }
  });

  sidecar.on("state", (info) => {
    for (const win of BrowserWindow.getAllWindows()) {
      win.webContents.send("engine:state", info);
    }
  });
  void sidecar.start();

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", (event) => {
  if (busy && !quitConfirmed) {
    event.preventDefault();
    const win = BrowserWindow.getAllWindows()[0];
    const clicked = dialog.showMessageBoxSync(win, {
      type: "warning",
      buttons: ["번역 중단하고 종료 / Quit anyway", "계속 작업 / Keep working"],
      defaultId: 1,
      cancelId: 1,
      title: "moru",
      message: "번역 작업이 진행 중입니다 / A translation job is running",
      detail:
        "지금 종료하면 번역이 중단됩니다. 완료된 번역은 저장되어 다음 실행 때 '이어서 번역'으로 계속할 수 있습니다.\nQuitting now interrupts the job; finished batches are saved and can be resumed on the next launch.",
    });
    if (clicked === 0) {
      quitConfirmed = true;
      app.quit();
    }
    return;
  }
  if (!shutdownStarted) {
    shutdownStarted = true;
    event.preventDefault();
    // quit() again rather than exit(): exit skips the `quit` event, and with
    // it electron-updater's install-on-quit of a downloaded update.
    void sidecar.stop().finally(() => app.quit());
  }
});

app.on("window-all-closed", () => {
  app.quit();
});
