/**
 * CLI: groundtruth validate <file>
 *
 * Validates a single file from the command line. Useful for:
 * - Testing GroundTruth on a specific file
 * - CI/CD integration (validate generated code before merge)
 * - Demo purposes (show what GroundTruth catches)
 */

export async function handleValidate(file: string): Promise<void> {
  // TODO: Initialize LSP + symbol graph for current project
  // TODO: Read file, run validator
  // TODO: Print results
  console.log(`Validating ${file}...`);
}
