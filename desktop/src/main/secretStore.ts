/**
 * secrets.json: API keys and the web login token.
 *
 * Every change is a read-modify-write of one small JSON file, so two
 * changes running side by side (login stores token and name with
 * Promise.all) would each write the copy they read and the later one would
 * drop the other's key. Changes therefore run one at a time, and each lands
 * through a temp file + rename so a crash mid-write never leaves a
 * truncated file behind.
 *
 * A file that exists but no longer parses is moved aside rather than read as
 * empty: reading it as `{}` meant the next save overwrote every key.
 */

import { readFile, rename, writeFile } from "node:fs/promises";

export class SecretsCorruptError extends Error {}

export class SecretStore {
  readonly #path: string;
  #queue: Promise<unknown> = Promise.resolve();

  constructor(path: string) {
    this.#path = path;
  }

  /** The stored map; `{}` only when there is no file yet. */
  async read(): Promise<Record<string, string>> {
    let raw: string;
    try {
      raw = await readFile(this.#path, "utf-8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
      throw error;
    }
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, string>;
      }
    } catch {
      // fall through
    }
    const aside = `${this.#path}.corrupt-${Date.now()}`;
    await rename(this.#path, aside).catch(() => undefined);
    throw new SecretsCorruptError(`secrets.json could not be read; kept as ${aside}`);
  }

  /** Apply `change` to the stored map, serialized behind earlier changes. */
  update(change: (store: Record<string, string>) => void): Promise<void> {
    const run = this.#queue.then(async () => {
      let store: Record<string, string>;
      try {
        store = await this.read();
      } catch (error) {
        // The unreadable file was moved aside: start over instead of
        // refusing every save forever. Its keys stay recoverable there.
        if (!(error instanceof SecretsCorruptError)) throw error;
        store = {};
      }
      change(store);
      const temp = `${this.#path}.tmp`;
      await writeFile(temp, JSON.stringify(store), { encoding: "utf-8", mode: 0o600 });
      await rename(temp, this.#path);
    });
    // A failed change must not wedge every later one.
    this.#queue = run.catch(() => undefined);
    return run;
  }
}
