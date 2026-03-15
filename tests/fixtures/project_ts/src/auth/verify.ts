/**
 * Token verification.
 */

import { decodeToken, isTokenExpired, TokenPayload } from "./jwt";
import { isTokenBlacklisted } from "./logout";
import { AuthenticationError } from "../utils/errors";

export async function verifyToken(token: string): Promise<TokenPayload> {
  if (!token || token.trim() === "") {
    throw new AuthenticationError("Token is required");
  }

  if (isTokenBlacklisted(token)) {
    throw new AuthenticationError("Token has been revoked");
  }

  if (isTokenExpired(token)) {
    throw new AuthenticationError("Token has expired");
  }

  try {
    const payload = decodeToken(token);
    return payload;
  } catch {
    throw new AuthenticationError("Invalid token");
  }
}
