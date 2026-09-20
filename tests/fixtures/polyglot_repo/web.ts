// TypeScript fixture: alias + param callback + Express route.
import express from "express";

const app = express();

export function tsHelper(): string {
  return "ok";
}

// Bare alias: callViaAlias() -> aliasFn() must resolve to tsHelper.
const aliasFn = tsHelper;

export function callViaAlias(): string {
  return aliasFn();
}

// Parameter callback: tsWrap(tsHelper) then cb() must flow arg->formal.
export function tsWrap(cb: () => string): string {
  return cb();
}

export function tsEntry(): string {
  return tsWrap(tsHelper);
}

function listUsers(_req: unknown, res: { json: (b: unknown) => void }): void {
  res.json({ users: callViaAlias() });
}

// HAR-90 middleware wiring: app.use(mw) / app.use(path, mw) must emit
// MIDDLEWARE_ON edges from the middleware function to this file's anchor.
function authMw(_req: unknown, _res: unknown, next: () => void): void {
  next();
}

function apiScope(_req: unknown, _res: unknown, next: () => void): void {
  next();
}

app.use(authMw);
app.use("/api", apiScope);

app.get("/api/users", listUsers);

// Field-access shapes: this.* reads/writes must carry access_sites.
class Registry {
  private total = 0;

  add(n: number): number {
    this.total += n;
    return this.total;
  }
}

export const registry = new Registry();

// HAR-90 CFG fixture: if/else + try/catch + loop in one exported function so
// the statement-level control-flow sidecar has a deterministic target.
export function flowExercise(limit: number): number {
  let total = 0;
  for (let i = 0; i < limit; i++) {
    if (i % 2 === 0) {
      total += i;
    } else {
      total -= 1;
    }
    try {
      total = Math.max(total, i);
    } catch (e) {
      total = -1;
    }
  }
  return total;
}
