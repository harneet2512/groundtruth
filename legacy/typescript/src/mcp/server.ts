/**
 * MCP Server — Registers tools, stdio transport (CLAUDE spec).
 * Tools: groundtruth_generate, groundtruth_validate, groundtruth_status.
 */
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import type { SymbolGraph } from '../symbol-graph/index.js';
import type { InterventionTracker } from '../stats/tracker.js';
import {
  handleGenerate,
  handleValidate,
  handleStatus,
  handleFindRelevant,
  handleTraceSymbol,
  validateGenerateArgs,
  validateValidateArgs,
  validateFindRelevantArgs,
  validateTraceSymbolArgs,
} from './tools.js';

export interface MCPServerDeps {
  symbolGraph: SymbolGraph;
  tracker: InterventionTracker;
}

function jsonContent(text: string, isError = false) {
  return { content: [{ type: 'text' as const, text }], isError };
}

export function createMCPServer(deps: MCPServerDeps) {
  const server = new McpServer(
    { name: 'groundtruth', version: '0.1.0' },
    { capabilities: { tools: {} } }
  );

  server.registerTool(
    'groundtruth_generate',
    {
      description:
        'Get a codebase briefing before writing code and/or validate proposed code. Send intent for briefing, proposed_code for validation, or both.',
      inputSchema: {
        intent: z.string().optional(),
        proposed_code: z.string().optional(),
        file_path: z.string().optional(),
      },
    },
    async (args) => {
      const validated = validateGenerateArgs(args);
      if (!validated.success) {
        return jsonContent(JSON.stringify({ error: validated.error }), true);
      }
      const result = await handleGenerate(validated.data, deps);
      return jsonContent(JSON.stringify(result));
    }
  );

  server.registerTool(
    'groundtruth_validate',
    {
      description: 'Validate code already written to disk. Safety net for when groundtruth_generate was skipped.',
      inputSchema: { file_path: z.string().min(1) },
    },
    async (args) => {
      const validated = validateValidateArgs(args);
      if (!validated.success) {
        return jsonContent(JSON.stringify({ error: validated.error }), true);
      }
      const result = await handleValidate(validated.data, deps);
      return jsonContent(JSON.stringify(result));
    }
  );

  server.registerTool(
    'groundtruth_status',
    {
      description: 'Check health: indexed symbols, intervention stats.',
      inputSchema: {},
    },
    async () => {
      const result = handleStatus(deps);
      return jsonContent(JSON.stringify(result));
    }
  );

  server.registerTool(
    'groundtruth_find_relevant',
    {
      description:
        'Given a task description, find which files in the codebase are relevant. Uses the import/call graph. Call this FIRST before reading any files. Pass entry_points to start traversal from known files.',
      inputSchema: {
        description: z.string().min(1),
        entry_points: z.array(z.string()).optional(),
        max_files: z.number().optional(),
      },
    },
    async (args) => {
      const validated = validateFindRelevantArgs(args);
      if (!validated.success) {
        return jsonContent(JSON.stringify({ error: validated.error }), true);
      }
      const result = handleFindRelevant(validated.data, deps);
      return jsonContent(JSON.stringify(result));
    }
  );

  server.registerTool(
    'groundtruth_trace_symbol',
    {
      description:
        'Trace a symbol through the codebase: who defines it, who imports/calls it, dependency chain, impact radius.',
      inputSchema: {
        symbol: z.string().min(1),
        direction: z.enum(['callers', 'callees', 'both']).optional(),
        max_depth: z.number().optional(),
      },
    },
    async (args) => {
      const validated = validateTraceSymbolArgs(args);
      if (!validated.success) {
        return jsonContent(JSON.stringify({ error: validated.error }), true);
      }
      const result = handleTraceSymbol(validated.data, deps);
      return jsonContent(JSON.stringify(result));
    }
  );

  return {
    async start() {
      const transport = new StdioServerTransport();
      await server.connect(transport);
    },
  };
}
