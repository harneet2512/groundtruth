/**
 * Auth module barrel export.
 *
 * NOTE: JWT functions (signToken, decodeToken) are intentionally NOT
 * re-exported here. Import them directly from "./jwt" if needed.
 * This is a deliberate design choice for testing barrel export behavior.
 */

export { login, LoginResult } from "./login";
export { logout } from "./logout";
export { verifyToken } from "./verify";
export type { TokenPayload } from "./jwt";
