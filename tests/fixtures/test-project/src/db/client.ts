export interface DbClient {
  query<T>(sql: string, params?: unknown[]): Promise<T[]>;
  close(): Promise<void>;
}
export function getDbClient(): DbClient {
  return { query: async () => [], close: async () => {} };
}
