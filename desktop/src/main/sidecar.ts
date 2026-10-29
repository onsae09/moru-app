/**
 * Python engine sidecar lifecycle.
 *
 * Startup handshake: pick a free loopback port, generate a
 * session token, spawn the sidecar with both, poll /health until ready.
 * On crash: restart up to MAX_RESTARTS, then surface a failed state. The
 * budget refills after STABLE_MS of healthy uptime, and the renderer can
 * ask for a fresh start from the failed screen (`restart()`).
 *
 * The engine's stdin is a pipe held by this process: when the app dies by
 * any means the OS closes it and the engine shuts itself down
 * (`--exit-with-stdin`), instead of running on as an orphan.
 *
 * Output goes to userData/logs/engine.log (rotated once at LOG_MAX_BYTES)
 * and the last LOG_TAIL_LINES lines are kept for the failed screen.
 *
 * Dev attach: when MORU_ENGINE_PORT + MORU_ENGINE_TOKEN are set, no process
 * is spawned - we attach to a manually started
 * `uv run python -m moru_engine.server`.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import { createWriteStream, existsSync, mkdirSync, renameSync, statSync, type WriteStream } from "node:fs";
import net from "node:net";
import path from "node:path";
import { StringDecoder } from "node:string_decoder";

import { app } from "electron";

import type { EngineInfo } from "../shared/bridge";

const MAX_RESTARTS = 3;
const HEALTH_TIMEOUT_MS = 30_000;
const HEALTH_INTERVAL_MS = 250;
const SHUTDOWN_GRACE_MS = 5_000;
/** Healthy uptime after which earlier crashes no longer count. */
const STABLE_MS = 10 * 60_000;
const LOG_MAX_BYTES = 5 * 1024 * 1024;
const LOG_TAIL_LINES = 200;

function delay(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
}

async function pickFreePort(): Promise<number> {
  const { promise, resolve, reject } = Promise.withResolvers<number>();
  const server = net.createServer();
  server.unref();
  server.on("error", reject);
  server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    if (address === null || typeof address === "string") {
      server.close();
      reject(new Error("could not determine free port"));
      return;
    }
    const { port } = address;
    server.close(() => resolve(port));
  });
  return promise;
}

interface SpawnPlan {
  command: string;
  args: string[];
  cwd?: string;
}

/**
 * Resolve how to launch the engine.
 * - packaged: bundled PyInstaller onedir under resources/engine/
 * - dev: `uv run python -m moru_engine.server` from the sibling engine repo dir
 */
function resolveSpawnPlan(port: number): SpawnPlan {
  // The session token travels in the environment (MORU_SESSION_TOKEN), not
  // argv: any local user can read another process's command line (`ps`).
  const serverArgs = ["--port", String(port), "--exit-with-stdin"];
  if (app.isPackaged) {
    const bin = process.platform === "win32" ? "moru-engine-server.exe" : "moru-engine-server";
    return {
      command: path.join(process.resourcesPath, "engine", bin),
      args: serverArgs,
    };
  }
  const engineCwd =
    process.env.MORU_ENGINE_DIR ?? path.resolve(app.getAppPath(), "..", "engine");
  return {
    command: "uv",
    args: ["run", "python", "-m", "moru_engine.server", ...serverArgs],
    cwd: engineCwd,
  };
}

export class EngineSidecar extends EventEmitter {
  #child: ChildProcess | null = null;
  #info: EngineInfo = { state: "starting", port: null, token: null, restarts: 0 };
  #stopping = false;
  #attached = false;
  /** Bumped per spawn: only the CURRENT child's exit may trigger a restart. */
  #generation = 0;
  #readyAt: number | null = null;
  #log: WriteStream | null = null;
  #tail: string[] = [];

  get info(): EngineInfo {
    return this.#info;
  }

  /** The last lines the engine printed, for "copy log" on the failed screen. */
  get logTail(): string {
    return this.#tail.join("\n");
  }

  #setInfo(patch: Partial<EngineInfo>): void {
    this.#info = { ...this.#info, ...patch };
    this.emit("state", this.#info);
  }

  #openLog(): void {
    if (this.#log !== null) return;
    try {
      const dir = path.join(app.getPath("userData"), "logs");
      mkdirSync(dir, { recursive: true });
      const file = path.join(dir, "engine.log");
      if (existsSync(file) && statSync(file).size > LOG_MAX_BYTES) {
        renameSync(file, `${file}.1`);
      }
      this.#log = createWriteStream(file, { flags: "a" });
      this.#log.on("error", () => {
        this.#log = null;
      });
    } catch {
      this.#log = null;
    }
  }

  #record(stream: "out" | "err", text: string): void {
    // NOTE: session token never appears in engine logs (engine convention).
    const trimmed = text.trimEnd();
    if (trimmed === "") return;
    if (stream === "out") console.log(`[engine] ${trimmed}`);
    else console.error(`[engine] ${trimmed}`);
    this.#log?.write(`${trimmed}\n`);
    this.#tail.push(...trimmed.split(/\r?\n/));
    if (this.#tail.length > LOG_TAIL_LINES) this.#tail.splice(0, this.#tail.length - LOG_TAIL_LINES);
  }

  async start(): Promise<void> {
    const envPort = process.env.MORU_ENGINE_PORT;
    const envToken = process.env.MORU_ENGINE_TOKEN;
    if (envPort !== undefined && envToken !== undefined) {
      this.#attached = true;
      const port = Number(envPort);
      this.#setInfo({ state: "starting", port, token: envToken });
      await this.#waitHealthy(port, null);
      this.#setInfo({ state: "ready" });
      return;
    }
    this.#openLog();
    await this.#spawnOnce();
  }

  /** Start over from the failed screen with a fresh restart budget. */
  async restart(): Promise<void> {
    if (this.#attached || this.#stopping || this.#info.state !== "failed") return;
    this.#generation += 1; // a straggling exit of the old child is ignored
    this.#child?.kill();
    this.#child = null;
    this.#setInfo({ restarts: 0, error: undefined });
    await this.#spawnOnce();
  }

  async #spawnOnce(): Promise<void> {
    const generation = ++this.#generation;
    const port = await pickFreePort();
    const token = randomBytes(32).toString("hex");
    const plan = resolveSpawnPlan(port);
    if (app.isPackaged && !existsSync(plan.command)) {
      this.#setInfo({ state: "failed", error: `engine binary missing: ${plan.command}` });
      return;
    }
    this.#setInfo({ state: this.#info.restarts > 0 ? "restarting" : "starting", port, token });

    const env: NodeJS.ProcessEnv = {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      // A Korean Windows console defaults to cp949: piped output would be
      // mangled, and printing a character cp949 lacks raises.
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      MORU_SESSION_TOKEN: token,
    };
    if (app.isPackaged) {
      // Frozen builds resolve optimized-prompt artifacts via env override
      // (engine artifacts_dir()); the repo-layout fallback is meaningless
      // inside an onedir bundle.
      env.MORU_ARTIFACTS_DIR = path.join(process.resourcesPath, "engine", "artifacts");
    }
    const child = spawn(plan.command, plan.args, {
      cwd: plan.cwd,
      stdio: ["pipe", "pipe", "pipe"],
      env,
    });
    this.#child = child;
    // Chunks can split a multi-byte character; a decoder holds the remainder.
    const outDecoder = new StringDecoder("utf8");
    const errDecoder = new StringDecoder("utf8");
    child.stdout?.on("data", (chunk: Buffer) => this.#record("out", outDecoder.write(chunk)));
    child.stderr?.on("data", (chunk: Buffer) => this.#record("err", errDecoder.write(chunk)));
    child.stdin?.on("error", () => undefined); // EPIPE once the engine is gone
    child.on("error", (error) => this.#record("err", `spawn failed: ${String(error)}`));
    let healthy = false;
    child.on("exit", (code) => {
      if (generation !== this.#generation) return; // superseded child
      this.#child = null;
      if (this.#stopping) return;
      this.#record("err", `exited unexpectedly with code ${code}`);
      // Before /health answered, the startup wait sees the exit and owns
      // the restart; doing it here too would run two in parallel.
      if (healthy) void this.#restart();
    });

    try {
      await this.#waitHealthy(port, child);
    } catch (error) {
      if (generation !== this.#generation || this.#stopping) return;
      // Health never came up. Retire this child first so its exit does not
      // start a second, parallel restart, then go through the budget once.
      this.#generation += 1;
      child.kill();
      this.#child = null;
      this.#record("err", `health check failed: ${String(error)}`);
      await this.#restart();
      return;
    }
    if (generation !== this.#generation) return;
    healthy = true;
    this.#readyAt = Date.now();
    this.#setInfo({ state: "ready" });
  }

  async #restart(): Promise<void> {
    if (this.#readyAt !== null && Date.now() - this.#readyAt >= STABLE_MS) {
      // Hours of healthy uptime since: an old crash says nothing now.
      this.#info = { ...this.#info, restarts: 0 };
    }
    this.#readyAt = null;
    if (this.#info.restarts >= MAX_RESTARTS) {
      const lastLines = this.#tail.slice(-5).join("\n");
      this.#setInfo({
        state: "failed",
        error:
          `engine crashed ${this.#info.restarts + 1} times; giving up` +
          (lastLines !== "" ? `\n${lastLines}` : ""),
      });
      return;
    }
    this.#setInfo({ restarts: this.#info.restarts + 1, state: "restarting" });
    await delay(500);
    if (!this.#stopping) await this.#spawnOnce();
  }

  async #waitHealthy(port: number, child: ChildProcess | null): Promise<void> {
    const deadline = Date.now() + HEALTH_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (this.#stopping) throw new Error("sidecar stopped during startup");
      // A binary that dies at once (a missing DLL) must not be polled for
      // the whole timeout.
      if (child !== null && child.exitCode !== null) {
        throw new Error(`engine exited during startup with code ${child.exitCode}`);
      }
      try {
        const res = await fetch(`http://127.0.0.1:${port}/health`, {
          signal: AbortSignal.timeout(1_000),
        });
        if (res.ok) return;
      } catch {
        // not up yet
      }
      await delay(HEALTH_INTERVAL_MS);
    }
    throw new Error(`engine /health not ready within ${HEALTH_TIMEOUT_MS}ms`);
  }

  /** Graceful shutdown: POST /shutdown, then SIGKILL after a grace period. */
  async stop(): Promise<void> {
    this.#stopping = true;
    const { port, token } = this.#info;
    const child = this.#child;
    if (this.#attached || port === null || token === null) return;
    try {
      await fetch(`http://127.0.0.1:${port}/shutdown`, {
        method: "POST",
        headers: { authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(2_000),
      });
    } catch {
      // engine already gone or unresponsive; fall through to kill
    }
    if (child === null || child.exitCode !== null) return;
    const { promise, resolve } = Promise.withResolvers<void>();
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve();
    }, SHUTDOWN_GRACE_MS);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    await promise;
    this.#log?.end();
  }
}
