/**
 * Logout functionality.
 */

import { decodeToken } from "./jwt";

// In-memory token blacklist (in production, use Redis or similar)
const tokenBlacklist: Set<string> = new Set();

export async function logout(token: string): Promise<void> {
  // Verify the token is valid before blacklisting
  const payload = decodeToken(token);
  if (!payload) {
    throw new Error("Invalid token");
  }
  tokenBlacklist.add(token);
}

export function isTokenBlacklisted(token: string): boolean {
  return tokenBlacklist.has(token);
}

export function clearBlacklist(): void {
  tokenBlacklist.clear();
}
