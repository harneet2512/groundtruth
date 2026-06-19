#!/usr/bin/env node
/**
 * CLI Entry Point — Routes commands to handlers.
 *
 * Commands:
 * - groundtruth validate <file>  — validate a file from command line
 * - groundtruth setup --claude-code | --cursor  — auto-configure MCP
 * - groundtruth status  — show indexed symbols + LSP + stats summary
 * - groundtruth stats [--json] [--days N] [--file GLOB]  — detailed stats
 */

import { Command } from "commander";
import {
  handleValidate,
  handleSetup,
  handleStatus,
  handleStats,
  handleIndex,
} from "./commands/index.js";

const program = new Command();

program
  .name("groundtruth")
  .description("MCP server that grounds AI code generation in codebase reality")
  .version("0.1.0");

program
  .command("index")
  .description("Index the project (extract symbols to SQLite)")
  .action(handleIndex);

program
  .command("validate <file>")
  .description("Validate a file against the codebase")
  .action(handleValidate);

program
  .command("setup")
  .description("Configure GroundTruth for your AI coding tool")
  .option("--claude-code", "Configure for Claude Code")
  .option("--cursor", "Configure for Cursor")
  .action((opts) => handleSetup({ cursor: opts.cursor, claudeCode: opts.claudeCode }));

program
  .command("status")
  .description("Show GroundTruth status and stats summary")
  .action(handleStatus);

program
  .command("stats")
  .description("Show detailed intervention statistics")
  .option("--json", "Output as JSON")
  .option("--days <n>", "Show stats for last N days", "7")
  .option("--file <glob>", "Filter by file pattern")
  .action(handleStats);

program.parse();
