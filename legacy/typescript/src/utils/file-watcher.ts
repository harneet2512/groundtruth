/**
 * File watcher for re-indexing on change.
 * TODO: Use chokidar; on change call indexer.indexFile(filePath).
 */
export function startFileWatcher(
  _projectRoot: string,
  _onChange: (filePath: string) => void
): () => void {
  return () => {};
}
