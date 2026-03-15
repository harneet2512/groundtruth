/**
 * GroundTruth MCP Server — Entry Point (CLAUDE spec).
 * Stdio transport; no daemon. Client spawns this process.
 */
import { createMCPServer } from './mcp/server.js';
import { SymbolGraph, ProjectIndexer } from './symbol-graph/index.js';
import { InterventionTracker } from './stats/tracker.js';
import { logger } from './utils/logger.js';

async function main() {
  const projectRoot = process.cwd();
  logger.info(`Starting GroundTruth for ${projectRoot}`);

  const symbolGraph = new SymbolGraph(projectRoot);
  const indexer = new ProjectIndexer(symbolGraph.store);
  await indexer.indexProject(projectRoot);

  const tracker = new InterventionTracker(symbolGraph.db);
  const server = createMCPServer({ symbolGraph, tracker });
  await server.start();

  logger.info(`GroundTruth ready — ${symbolGraph.symbolCount} symbols indexed`);
}

main().catch((err) => {
  logger.error('Failed to start GroundTruth', err);
  process.exit(1);
});
