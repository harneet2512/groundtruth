/**
 * Test fixture: Example route file that imports auth middleware.
 * Multiple route files like this establish the usage pattern.
 */

import authMiddleware from "../middleware/auth.js";
import type { JWTPayload } from "../auth/login.js";

export function registerUserRoutes(app: any): void {
  app.get("/users/me", authMiddleware, (req: any, res: any) => {
    const user: JWTPayload = req.user;
    res.json({ user });
  });
}
