/**
 * Test fixture: Auth middleware.
 * Demonstrates the pattern that 12/14 route files follow.
 */

import { verifyToken } from "../auth/login.js";
import type { JWTPayload } from "../auth/login.js";

export default async function authMiddleware(
  req: any,
  res: any,
  next: any
): Promise<void> {
  const token = req.headers.authorization?.split(" ")[1];
  if (!token) {
    res.status(401).json({ error: "No token provided" });
    return;
  }

  try {
    const payload: JWTPayload = await verifyToken(token);
    req.user = payload;
    next();
  } catch {
    res.status(401).json({ error: "Invalid token" });
  }
}
