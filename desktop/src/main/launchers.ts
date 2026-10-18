/**
 * Where each launcher keeps its instances, and which of them are modpacks
 * moru can open.
 *
 * The defaults follow each launcher's own layout per OS. Players who moved
 * that folder — CurseForge's "modding folder" on another drive, a portable
 * MultiMC — set it per launcher in Settings, and that path replaces the
 * default; without it auto-detect found nothing for them.
 */

import { existsSync, readdirSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  LAUNCHER_IDS,
  type DetectedInstance,
  type LauncherId,
  type LauncherPaths,
  type LauncherRoot,
} from "../shared/bridge";

/** Inside an instance, the folder that holds mods/ (Prism, MultiMC). */
const GAME_SUBDIRS: Record<LauncherId, readonly string[]> = {
  CurseForge: [],
  Modrinth: [],
  Prism: [".minecraft", "minecraft"],
  MultiMC: [".minecraft", "minecraft"],
};

export interface PlatformEnv {
  platform: NodeJS.Platform;
  home: string;
  env: Record<string, string | undefined>;
}

export function currentPlatform(): PlatformEnv {
  return { platform: process.platform, home: os.homedir(), env: process.env };
}

/** Each launcher's own instance folder on this OS; null where it has none. */
export function defaultLauncherRoots({
  platform,
  home,
  env,
}: PlatformEnv): Record<LauncherId, string | null> {
  // The target OS's path rules, so defaults come out right wherever computed.
  const p = platform === "win32" ? path.win32 : path.posix;
  if (platform === "win32") {
    const appData = env.APPDATA || p.join(home, "AppData", "Roaming");
    return {
      CurseForge: p.join(home, "curseforge", "minecraft", "Instances"),
      Modrinth: p.join(appData, "ModrinthApp", "profiles"),
      Prism: p.join(appData, "PrismLauncher", "instances"),
      MultiMC: null,
    };
  }
  if (platform === "darwin") {
    const support = p.join(home, "Library", "Application Support");
    return {
      CurseForge: p.join(home, "Documents", "curseforge", "minecraft", "Instances"),
      Modrinth: p.join(support, "ModrinthApp", "profiles"),
      Prism: p.join(support, "PrismLauncher", "instances"),
      MultiMC: null,
    };
  }
  const data = env.XDG_DATA_HOME || p.join(home, ".local", "share");
  return {
    CurseForge: null,
    Modrinth: p.join(data, "ModrinthApp", "profiles"),
    Prism: p.join(data, "PrismLauncher", "instances"),
    MultiMC: p.join(data, "multimc", "instances"),
  };
}

export function launcherRoots(env: PlatformEnv = currentPlatform()): LauncherRoot[] {
  const defaults = defaultLauncherRoots(env);
  return LAUNCHER_IDS.map((launcher) => ({ launcher, defaultPath: defaults[launcher] }));
}

/**
 * The overrides that can name a folder: a known launcher and an absolute
 * path. The value arrives over IPC from persisted renderer settings, so
 * anything else is dropped rather than scanned relative to the app's cwd.
 */
export function sanitizeLauncherPaths(
  value: unknown,
  platform: NodeJS.Platform = process.platform,
): LauncherPaths {
  if (typeof value !== "object" || value === null) return {};
  const p = platform === "win32" ? path.win32 : path.posix;
  const entries = value as Record<string, unknown>;
  const result: LauncherPaths = {};
  for (const launcher of LAUNCHER_IDS) {
    const raw = entries[launcher];
    if (typeof raw !== "string") continue;
    const trimmed = raw.trim();
    if (trimmed.length > 0 && p.isAbsolute(trimmed)) result[launcher] = trimmed;
  }
  return result;
}

/** Instance folders directly under `root` whose game dir has mods/. */
export function listInstanceDirs(root: string, launcher: LauncherId): DetectedInstance[] {
  if (!existsSync(root)) return [];
  try {
    return readdirSync(root, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => {
        const base = path.join(root, d.name);
        const gameDir =
          GAME_SUBDIRS[launcher].map((s) => path.join(base, s)).find(existsSync) ?? base;
        return { launcher, name: d.name, path: gameDir };
      })
      .filter((inst) => existsSync(path.join(inst.path, "mods")));
  } catch {
    return [];
  }
}

export function detectInstances(
  overrides: unknown,
  env: PlatformEnv = currentPlatform(),
): DetectedInstance[] {
  const defaults = defaultLauncherRoots(env);
  const custom = sanitizeLauncherPaths(overrides, env.platform);
  const seen = new Set<string>();
  return LAUNCHER_IDS.flatMap((launcher) => {
    const root = custom[launcher] ?? defaults[launcher];
    return root === null ? [] : listInstanceDirs(root, launcher);
  }).filter((inst) => {
    // Two launchers pointed at one folder list it once.
    if (seen.has(inst.path)) return false;
    seen.add(inst.path);
    return true;
  });
}
