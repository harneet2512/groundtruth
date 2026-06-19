/**
 * Validator orchestrator — runs import, package, signature validators;
 * attaches Levenshtein + cross-index fix suggestions to errors.
 */
import type { SymbolStore } from '../symbol-graph/sqlite-store.js';
import { searchSymbolByName } from '../symbol-graph/query.js';
import { validateImports, type ImportError } from './import-validator.js';
import { validatePackages, type PackageError } from './package-validator.js';
import { validateSignatures, type SignatureError } from './signature-validator.js';

export interface Fix {
  suggestion: string;
  source: 'levenshtein' | 'cross_index';
  confidence: 'high' | 'medium' | 'low';
}

export type ValidationError = (ImportError | PackageError | SignatureError) & {
  fix?: Fix;
};

export interface ValidationResult {
  valid: boolean;
  errors: ValidationError[];
}

function attachFixes(
  store: SymbolStore,
  errors: Array<ImportError | PackageError | SignatureError>
): ValidationError[] {
  return errors.map((err): ValidationError => {
    if (err.type === 'symbol_not_found') {
      const importErr = err as ImportError;

      if (importErr.suggestions && importErr.suggestions.length > 0) {
        const distance = importErr.bestDistance ?? 1;

        // Low confidence (distance >= 3): also try cross-index, prefer it if found
        if (distance >= 3) {
          const crossResults = searchSymbolByName(store, err.symbol);
          if (crossResults.length > 0) {
            const best = crossResults[0];
            return {
              ...err,
              fix: {
                suggestion: `'${err.symbol}' is exported from '${best.module_path}' (${best.file_path}).`,
                source: 'cross_index',
                confidence: 'high',
              },
            };
          }
        }

        const confidence = distance <= 1 ? 'high' : distance <= 2 ? 'medium' : 'low';
        return {
          ...err,
          fix: {
            suggestion: `Did you mean '${importErr.suggestions[0]}'?`,
            source: 'levenshtein',
            confidence,
          },
        };
      }

      // Cross-index: search for the symbol in other modules
      const crossResults = searchSymbolByName(store, err.symbol);
      if (crossResults.length > 0) {
        const best = crossResults[0];
        return {
          ...err,
          fix: {
            suggestion: `'${err.symbol}' is exported from '${best.module_path}' (${best.file_path}).`,
            source: 'cross_index',
            confidence: 'high',
          },
        };
      }
      return err;
    }

    if (err.type === 'module_not_found') {
      // Cross-index: look for any symbol with that name elsewhere
      const crossResults = searchSymbolByName(store, err.symbol);
      if (crossResults.length > 0) {
        const best = crossResults[0];
        return {
          ...err,
          fix: {
            suggestion: `'${err.symbol}' is exported from '${best.module_path}' (${best.file_path}).`,
            source: 'cross_index',
            confidence: 'medium',
          },
        };
      }
      return err;
    }

    if (err.type === 'signature_mismatch') {
      const sigErr = err as SignatureError;
      if (sigErr.expectedSignature) {
        return {
          ...err,
          fix: {
            suggestion: `Expected signature: ${err.symbol}${sigErr.expectedSignature}`,
            source: 'levenshtein',
            confidence: 'high',
          },
        };
      }
      // Fallback: reconstruct from params in store
      const symbols = store.getExportedSymbolsByName(err.symbol);
      if (symbols.length > 0 && symbols[0].params) {
        try {
          const params = JSON.parse(symbols[0].params) as Array<{ name: string; type: string; optional?: boolean }>;
          const paramStr = params.map(p => `${p.name}${p.optional ? '?' : ''}: ${p.type}`).join(', ');
          const sig = symbols[0].return_type ? `(${paramStr}) => ${symbols[0].return_type}` : `(${paramStr})`;
          return {
            ...err,
            fix: {
              suggestion: `Expected signature: ${err.symbol}${sig}`,
              source: 'levenshtein',
              confidence: 'high',
            },
          };
        } catch { /* fall through */ }
      }
      return err;
    }

    // package_not_installed — no fix
    return err;
  });
}

export function validate(
  store: SymbolStore,
  code: string,
  filePath: string
): ValidationResult {
  const rawErrors: Array<ImportError | PackageError | SignatureError> = [
    ...validateImports(store, code, filePath),
    ...validatePackages(store, code, filePath),
    ...validateSignatures(store, code, filePath),
  ];
  const errors = attachFixes(store, rawErrors);
  return {
    valid: errors.length === 0,
    errors,
  };
}

export { validateImports } from './import-validator.js';
export { validatePackages } from './package-validator.js';
export { validateSignatures } from './signature-validator.js';
