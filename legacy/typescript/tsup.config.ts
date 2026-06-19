import { defineConfig } from 'tsup';

export default defineConfig({
  entry: {
    index: 'src/index.ts',
    'bin/cli': 'bin/cli.ts',
    'mcp/server': 'src/mcp/server.ts',
  },
  format: ['esm'],
  target: 'node18',
  dts: true,
  clean: true,
  splitting: false,
  sourcemap: true,
  shims: true,
});
