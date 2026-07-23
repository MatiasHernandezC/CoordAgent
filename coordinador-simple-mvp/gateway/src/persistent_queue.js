import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export class PersistentQueue {
  constructor(path, limit = 500) {
    this.path = path;
    this.deadLetterPath = path.replace(/\.json$/i, ".dead-letter.json");
    this.limit = limit;
    this.items = this.#load(this.path);
    this.deadLetters = this.#load(this.deadLetterPath);
  }

  get size() {
    return this.items.length;
  }

  get deadLetterSize() {
    return this.deadLetters.length;
  }

  has(id) {
    return Boolean(id) && this.items.some((item) => item.id === id);
  }

  enqueue(item) {
    if (!item?.id) throw new Error("mensaje pendiente sin id");
    if (this.has(item.id)) return false;
    if (this.items.length >= this.limit) {
      this.#appendDeadLetter(item, "cola persistente llena al recibir el mensaje");
      throw new Error(`cola persistente llena (${this.limit})`);
    }

    this.items.push({
      ...item,
      attempts: Number(item.attempts) || 0,
      nextAttemptAt: Number(item.nextAttemptAt) || Date.now(),
      queuedAt: item.queuedAt || new Date().toISOString(),
      lastError: item.lastError || null
    });
    this.#save();
    return true;
  }

  nextReady(now = Date.now()) {
    return this.#groupHeads().find((item) => item.nextAttemptAt <= now) ?? null;
  }

  millisecondsUntilNext(now = Date.now()) {
    const heads = this.#groupHeads();
    if (heads.length === 0) return null;
    return Math.max(0, Math.min(...heads.map((item) => item.nextAttemptAt)) - now);
  }

  markFailed(id, error, minDelayMs, maxDelayMs) {
    const item = this.items.find((candidate) => candidate.id === id);
    if (!item) return null;

    item.attempts += 1;
    const delayMs = Math.min(maxDelayMs, minDelayMs * 2 ** Math.max(0, item.attempts - 1));
    item.nextAttemptAt = Date.now() + delayMs;
    item.lastError = String(error).slice(0, 500);
    this.#save();
    return { ...item, delayMs };
  }

  remove(id) {
    const before = this.items.length;
    this.items = this.items.filter((item) => item.id !== id);
    if (this.items.length === before) return false;
    this.#save();
    return true;
  }

  markProgress(id, progress) {
    const item = this.items.find((candidate) => candidate.id === id);
    if (!item) return null;
    Object.assign(item, progress);
    this.#save();
    return { ...item };
  }

  moveToDeadLetter(id, error) {
    const item = this.items.find((candidate) => candidate.id === id);
    if (!item) return false;
    this.#appendDeadLetter(item, error);
    return this.remove(id);
  }

  #appendDeadLetter(item, error) {
    this.deadLetters.push({
      ...item,
      deadLetterAt: new Date().toISOString(),
      lastError: String(error).slice(0, 500)
    });
    const deadLetterLimit = Math.max(1000, this.limit * 10);
    if (this.deadLetters.length > deadLetterLimit) {
      this.deadLetters = this.deadLetters.slice(-deadLetterLimit);
    }
    this.#write(this.deadLetterPath, this.deadLetters);
  }

  #groupHeads() {
    const seen = new Set();
    const heads = [];
    for (const item of this.items) {
      const key = item.jid || `__${item.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      heads.push(item);
    }
    return heads;
  }

  #load(path) {
    if (!existsSync(path)) return [];
    try {
      const parsed = JSON.parse(readFileSync(path, "utf8"));
      return Array.isArray(parsed) ? parsed.filter((item) => item?.id) : [];
    } catch {
      const preserved = `${path}.corrupt-${Date.now()}`;
      renameSync(path, preserved);
      return [];
    }
  }

  #save() {
    this.#write(this.path, this.items);
  }

  #write(path, value) {
    mkdirSync(dirname(path), { recursive: true });
    const temp = `${path}.tmp`;
    writeFileSync(temp, JSON.stringify(value, null, 2), { encoding: "utf8", mode: 0o600 });
    renameSync(temp, path);
  }
}
