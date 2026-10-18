import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, expect, test } from "bun:test";

import {
  defaultLauncherRoots,
  detectInstances,
  sanitizeLauncherPaths,
} from "./launchers.ts";

const temps = [];
afterEach(() => {
  for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function tempHome() {
  const dir = mkdtempSync(path.join(os.tmpdir(), "moru-launchers-"));
  temps.push(dir);
  return dir;
}

function instance(root, name, gameSubdir = "") {
  mkdirSync(path.join(root, name, gameSubdir, "mods"), { recursive: true });
}

test("default instance folders follow each launcher's layout per OS", () => {
  expect(
    defaultLauncherRoots({
      platform: "win32",
      home: "C:\\Users\\kim",
      env: { APPDATA: "D:\\Profiles\\kim\\Roaming" },
    }),
  ).toEqual({
    CurseForge: "C:\\Users\\kim\\curseforge\\minecraft\\Instances",
    Modrinth: "D:\\Profiles\\kim\\Roaming\\ModrinthApp\\profiles",
    Prism: "D:\\Profiles\\kim\\Roaming\\PrismLauncher\\instances",
    MultiMC: null,
  });
  // macOS keeps launcher data under Application Support, not ~/.local/share.
  expect(defaultLauncherRoots({ platform: "darwin", home: "/Users/kim", env: {} })).toEqual({
    CurseForge: "/Users/kim/Documents/curseforge/minecraft/Instances",
    Modrinth: "/Users/kim/Library/Application Support/ModrinthApp/profiles",
    Prism: "/Users/kim/Library/Application Support/PrismLauncher/instances",
    MultiMC: null,
  });
  expect(
    defaultLauncherRoots({ platform: "linux", home: "/home/kim", env: { XDG_DATA_HOME: "/data" } }),
  ).toEqual({
    CurseForge: null,
    Modrinth: "/data/ModrinthApp/profiles",
    Prism: "/data/PrismLauncher/instances",
    MultiMC: "/data/multimc/instances",
  });
});

test("only absolute folders of known launchers are scanned", () => {
  expect(
    sanitizeLauncherPaths(
      {
        CurseForge: "  D:\\Games\\CurseForge\\Instances  ",
        Prism: "relative\\instances",
        Modrinth: 42,
        ATLauncher: "C:\\ATLauncher\\instances",
      },
      "win32",
    ),
  ).toEqual({ CurseForge: "D:\\Games\\CurseForge\\Instances" });
  expect(sanitizeLauncherPaths("C:\\x", "win32")).toEqual({});
  expect(sanitizeLauncherPaths(null, "linux")).toEqual({});
});

test("a folder set for a launcher replaces its default", () => {
  const home = tempHome();
  const env = { platform: process.platform, home, env: {} };
  const defaults = defaultLauncherRoots(env);

  // CurseForge moved to another drive: the default folder still exists but
  // holds an old pack, and the moved folder is the one the user plays from.
  const moved = path.join(home, "Games", "CurseForge", "Instances");
  instance(moved, "All the Mods 10");
  mkdirSync(path.join(moved, "Not a modpack"), { recursive: true });
  if (defaults.CurseForge !== null) instance(defaults.CurseForge, "Old Pack");
  // Prism keeps the game dir one level down.
  instance(defaults.Prism, "Prominence II", ".minecraft");

  const found = detectInstances({ CurseForge: moved }, env);

  expect(found).toEqual([
    { launcher: "CurseForge", name: "All the Mods 10", path: path.join(moved, "All the Mods 10") },
    {
      launcher: "Prism",
      name: "Prominence II",
      path: path.join(defaults.Prism, "Prominence II", ".minecraft"),
    },
  ]);
});

test("two launchers pointed at one folder list each pack once", () => {
  const home = tempHome();
  const shared = path.join(home, "instances");
  instance(shared, "Pack");
  const found = detectInstances(
    { CurseForge: shared, MultiMC: shared },
    { platform: process.platform, home, env: {} },
  );
  expect(found.map((inst) => inst.path)).toEqual([path.join(shared, "Pack")]);
});
