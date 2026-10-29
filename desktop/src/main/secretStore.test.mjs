import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { expect, test } from "bun:test";

import { SecretStore } from "./secretStore.ts";

const fresh = () => path.join(mkdtempSync(path.join(tmpdir(), "moru-secrets-")), "secrets.json");

test("concurrent changes all land", async () => {
  const file = fresh();
  const store = new SecretStore(file);
  await Promise.all([
    store.update((s) => (s["account:token"] = "t")),
    store.update((s) => (s["account:name"] = "n")),
    store.update((s) => (s["apikey:openai"] = "k")),
  ]);
  expect(JSON.parse(readFileSync(file, "utf-8"))).toEqual({
    "account:token": "t",
    "account:name": "n",
    "apikey:openai": "k",
  });
});

test("a delete racing a set is not undone", async () => {
  const store = new SecretStore(fresh());
  await store.update((s) => (s.a = "1"));
  await Promise.all([store.update((s) => delete s.a), store.update((s) => (s.b = "2"))]);
  expect(await store.read()).toEqual({ b: "2" });
});

test("a corrupt file is kept aside instead of being overwritten", async () => {
  const file = fresh();
  writeFileSync(file, '{"apikey:openai": "k"', "utf-8"); // truncated mid-write
  const store = new SecretStore(file);
  await expect(store.read()).rejects.toThrow();
  const aside = readdirSync(path.dirname(file)).filter((f) => f.includes(".corrupt-"));
  expect(aside.length).toBe(1);
  await store.update((s) => (s.b = "2"));
  expect(await store.read()).toEqual({ b: "2" });
});

test("no file reads as empty", async () => {
  expect(await new SecretStore(fresh()).read()).toEqual({});
});
