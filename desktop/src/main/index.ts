/**
 * Electron main process: frameless window, engine sidecar lifecycle,
 * quit confirmation while a job is running.
 */

import path from "node:path";

import { BrowserWindow, app, dialog, ipcMain, shell } from "electron";

import { registerIpc } from "./ipc";
import { EngineSidecar } from "./sidecar";
import { registerUpdater } from "./updater";

const sidecar = new EngineSidecar();
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
    backgroundColor: "#0E1512",
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
    void sidecar.stop().finally(() => app.exit(0));
  }
});

app.on("window-all-closed", () => {
  app.quit();
});
