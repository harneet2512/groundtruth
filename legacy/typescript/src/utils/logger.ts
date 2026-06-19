/**
 * Logger — Simple console wrapper.
 *
 * Not pino, not winston — just console.log with a prefix.
 * MCP servers communicate via stdio, so logging goes to stderr
 * to avoid corrupting the MCP protocol stream.
 */

export const logger = {
  info(msg: string): void {
    console.error(`[groundtruth] ${msg}`);
  },
  warn(msg: string, err?: unknown): void {
    console.error(`[groundtruth] WARN: ${msg}`, err ?? "");
  },
  error(msg: string, err?: unknown): void {
    console.error(`[groundtruth] ERROR: ${msg}`, err ?? "");
  },
  debug(msg: string): void {
    if (process.env.GROUNDTRUTH_DEBUG) {
      console.error(`[groundtruth] DEBUG: ${msg}`);
    }
  },
};
