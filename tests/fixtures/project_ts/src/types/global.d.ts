/** Global type declarations for the project */

declare namespace NodeJS {
  interface ProcessEnv {
    NODE_ENV: "development" | "production" | "test";
    PORT: string;
    DATABASE_URL: string;
    JWT_SECRET: string;
    SALT_ROUNDS: string;
  }
}

/** Generic API response wrapper */
interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  timestamp: number;
}

/** Pagination parameters */
interface PaginationParams {
  page: number;
  limit: number;
  offset: number;
}

/** Paginated result */
interface PaginatedResult<T> {
  items: T[];
  total: number;
  page: number;
  totalPages: number;
}
