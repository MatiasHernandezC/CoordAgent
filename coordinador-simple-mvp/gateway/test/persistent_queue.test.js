import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { PersistentQueue } from "../src/persistent_queue.js";


test("persiste, deduplica y recupera mensajes", () => {
  const directory = mkdtempSync(join(tmpdir(), "tavi-queue-"));
  const path = join(directory, "pending.json");
  try {
    const queue = new PersistentQueue(path, 2);
    assert.equal(queue.enqueue({ id: "m1", text: "hola" }), true);
    assert.equal(queue.enqueue({ id: "m1", text: "duplicado" }), false);
    assert.equal(queue.size, 1);

    const reloaded = new PersistentQueue(path, 2);
    assert.equal(reloaded.size, 1);
    assert.equal(reloaded.nextReady().text, "hola");
    assert.equal(reloaded.remove("m1"), true);
    assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), []);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});


test("aplica backoff y conserva el error", () => {
  const directory = mkdtempSync(join(tmpdir(), "tavi-queue-"));
  const path = join(directory, "pending.json");
  try {
    const queue = new PersistentQueue(path);
    queue.enqueue({ id: "m2", text: "hola" });
    const failed = queue.markFailed("m2", new Error("backend offline"), 1000, 10000);

    assert.equal(failed.attempts, 1);
    assert.equal(failed.delayMs, 1000);
    assert.match(failed.lastError, /backend offline/);
    assert.equal(queue.nextReady(), null);
    assert.ok(queue.millisecondsUntilNext() > 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});


test("rechaza overflow sin borrar lo ya encolado", () => {
  const directory = mkdtempSync(join(tmpdir(), "tavi-queue-"));
  const path = join(directory, "pending.json");
  try {
    const queue = new PersistentQueue(path, 1);
    queue.enqueue({ id: "m1" });
    assert.throws(() => queue.enqueue({ id: "m2" }), /cola persistente llena/);
    assert.equal(queue.size, 1);
    assert.equal(queue.has("m1"), true);
    assert.equal(queue.deadLetterSize, 1);
    assert.equal(queue.deadLetters[0].id, "m2");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});


test("mantiene FIFO por grupo pero deja avanzar otros grupos", () => {
  const directory = mkdtempSync(join(tmpdir(), "tavi-queue-"));
  const path = join(directory, "pending.json");
  try {
    const queue = new PersistentQueue(path);
    queue.enqueue({ id: "a1", jid: "grupo-a", nextAttemptAt: Date.now() });
    queue.enqueue({ id: "a2", jid: "grupo-a", nextAttemptAt: Date.now() });
    queue.enqueue({ id: "b1", jid: "grupo-b", nextAttemptAt: Date.now() });
    queue.markFailed("a1", "backend offline", 60000, 60000);

    assert.equal(queue.nextReady().id, "b1");
    queue.remove("b1");
    assert.equal(queue.nextReady(), null);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});


test("persiste progreso y mueve fallos permanentes a dead letter", () => {
  const directory = mkdtempSync(join(tmpdir(), "tavi-queue-"));
  const path = join(directory, "pending.json");
  try {
    const queue = new PersistentQueue(path);
    queue.enqueue({ id: "m3", jid: "grupo-a" });
    queue.markProgress("m3", { primarySent: true });
    queue.moveToDeadLetter("m3", "payload invalido");

    const reloaded = new PersistentQueue(path);
    assert.equal(reloaded.size, 0);
    assert.equal(reloaded.deadLetterSize, 1);
    assert.equal(reloaded.deadLetters[0].primarySent, true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
