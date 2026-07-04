/**
 * Authentication middleware.
 */

import { Request, Response, NextFunction } from "express";
import { decodeToken, TokenPayload } from "../auth/jwt";
import { isTokenBlacklisted } from "../auth/logout";

// Extend Express Request to include user info
declare global {
  namespace Express {
    interface Request {
      user?: TokenPayload;
    }
  }
}

export function authMiddleware(req: Request, res: Response, next: NextFunction): void {
  const authHeader = req.headers.authorization;

  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    res.status(401).json({ error: "No token provided" });
    return;
  }

  const token = authHeader.substring(7);

  if (isTokenBlacklisted(token)) {
    res.status(401).json({ error: "Token has been revoked" });
    return;
  }

  try {
    const payload = decodeToken(token);
    req.user = payload;
    next();
  } catch {
    res.status(401).json({ error: "Invalid or expired token" });
  }
}

export function requireAuth(req: Request, res: Response, next: NextFunction): void {
  if (!req.user) {
    res.status(401).json({ error: "Authentication required" });
    return;
  }
  next();
}
