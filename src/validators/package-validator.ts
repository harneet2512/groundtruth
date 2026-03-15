/**
 * Package validator — check non-relative imports against packages table.
 * Skips Node.js built-ins and relative imports.
 */
import type { SymbolStore } from '../symbol-graph/sqlite-store.js';

export interface PackageError {
  type: 'package_not_installed';
  symbol: string;
  location: string;
  message: string;
}

const NODE_BUILTINS = new Set([
  'assert', 'async_hooks', 'buffer', 'child_process', 'cluster',
  'console', 'constants', 'crypto', 'dgram', 'diagnostics_channel',
  'dns', 'domain', 'events', 'fs', 'http', 'http2', 'https',
  'inspector', 'module', 'net', 'os', 'path', 'perf_hooks',
  'process', 'punycode', 'querystring', 'readline', 'repl',
  'stream', 'string_decoder', 'sys', 'timers', 'tls', 'trace_events',
  'tty', 'url', 'util', 'v8', 'vm', 'wasi', 'worker_threads', 'zlib',
]);

function extractPackageName(specifier: string): string {
  if (specifier.startsWith('@')) {
    const parts = specifier.split('/');
    return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : specifier;
  }
  return specifier.split('/')[0];
}

function parseImportSources(code: string): string[] {
  const sources: string[] = [];
  const re = /import\s+(?:\{[^}]*\}|\w+|\*\s+as\s+\w+)\s+from\s+['"]([^'"]+)['"]/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(code)) !== null) {
    sources.push(match[1]);
  }
  return sources;
}

export function validatePackages(
  store: SymbolStore,
  code: string,
  filePath: string
): PackageError[] {
  const errors: PackageError[] = [];
  const sources = parseImportSources(code);

  for (const source of sources) {
    // Skip relative imports
    if (source.startsWith('.') || source.startsWith('/')) continue;

    // Skip node: protocol
    if (source.startsWith('node:')) continue;

    const packageName = extractPackageName(source);

    // Skip Node.js built-ins
    if (NODE_BUILTINS.has(packageName)) continue;

    const pkg = store.getPackage(packageName);
    if (!pkg) {
      errors.push({
        type: 'package_not_installed',
        symbol: packageName,
        location: filePath,
        message: `Package '${packageName}' is not listed in package.json.`,
      });
    }
  }

  return errors;
}
