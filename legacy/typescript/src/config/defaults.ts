/**
 * Default configuration values.
 */
export const defaultConfig = {
  dbPath: '.groundtruth/graph.sqlite',
  maxLevenshteinDistance: 3,
  ftsLimit: 20,
} as const;
