export class AppError extends Error {
  constructor(public statusCode: number, message: string) {
    super(message);
  }
}
export class NotFoundError extends AppError {
  constructor(resource: string) {
    super(404, `${resource} not found`);
  }
}
export class ValidationError extends AppError {
  constructor(message: string) {
    super(400, message);
  }
}
