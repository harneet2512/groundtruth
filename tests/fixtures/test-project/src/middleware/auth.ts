import type { Request, Response, NextFunction } from 'express';
import { verifyToken } from '../auth/verify';
export function authMiddleware(req: Request, res: Response, next: NextFunction): void {
  next();
}
