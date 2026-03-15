/**
 * JWT token operations.
 */

import * as jsonwebtoken from "jsonwebtoken";

export interface TokenPayload {
  userId: number;
  email: string;
  iat?: number;
  exp?: number;
}

const JWT_SECRET = process.env.JWT_SECRET || "default-secret-change-me";
const TOKEN_EXPIRY = "24h";

export function signToken(payload: object): string {
  const token = jsonwebtoken.sign(payload, JWT_SECRET, { expiresIn: TOKEN_EXPIRY });
  return token;
}

export function decodeToken(token: string): TokenPayload {
  const decoded = jsonwebtoken.verify(token, JWT_SECRET) as TokenPayload;
  return decoded;
}

export function isTokenExpired(token: string): boolean {
  try {
    const decoded = decodeToken(token);
    if (!decoded.exp) {
      return false;
    }
    return Date.now() >= decoded.exp * 1000;
  } catch {
    return true;
  }
}
