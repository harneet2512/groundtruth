/**
 * Global error handling middleware.
 */

import { Request, Response, NextFunction } from "express";
import { AppError, ValidationError } from "../utils/errors";

export interface ErrorResponse {
  error: string;
  statusCode: number;
  fields?: Record<string, string>;
  stack?: string;
}

export function errorHandler(
  err: Error,
  req: Request,
  res: Response,
  next: NextFunction
): void {
  const isDev = process.env.NODE_ENV === "development";

  if (err instanceof ValidationError) {
    const response: ErrorResponse = {
      error: err.message,
      statusCode: err.statusCode,
      fields: err.fields,
    };
    res.status(err.statusCode).json(response);
    return;
  }

  if (err instanceof AppError) {
    const response: ErrorResponse = {
      error: err.message,
      statusCode: err.statusCode,
      stack: isDev ? err.stack : undefined,
    };
    res.status(err.statusCode).json(response);
    return;
  }

  // Unhandled errors
  const response: ErrorResponse = {
    error: isDev ? err.message : "Internal server error",
    statusCode: 500,
    stack: isDev ? err.stack : undefined,
  };
  res.status(500).json(response);
}

export function notFoundHandler(req: Request, res: Response): void {
  res.status(404).json({
    error: `Route ${req.method} ${req.path} not found`,
    statusCode: 404,
  });
}
