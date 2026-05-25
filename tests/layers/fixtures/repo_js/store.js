// Tiny key-value store with TTL eviction. Used by the API client.

class Store {
  constructor(ttlMs) {
    this.ttlMs = ttlMs || 60000;
    this.data = new Map();
  }

  set(key, value) {
    this.data.set(key, { value, ts: Date.now() });
  }

  get(key) {
    const entry = this.data.get(key);
    if (!entry) return undefined;
    if (Date.now() - entry.ts > this.ttlMs) {
      this.data.delete(key);
      return undefined;
    }
    return entry.value;
  }

  size() {
    return this.data.size;
  }
}

module.exports = { Store };
