import type { Request, Response, NextFunction } from 'express';
import { AppError } from '../utils/errors.js';
export function errorHandler(err: Error, req: Request, res: Response, next: NextFunction): void {
  const status = err instanceof AppError ? err.statusCode : 500;
  res.status(status).json({ error: err.message });
}
