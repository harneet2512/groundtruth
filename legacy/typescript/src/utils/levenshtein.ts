/**
 * Levenshtein distance and suggestion helpers.
 * Used by validators for close-name suggestions (edit distance ≤ 3).
 */
export function levenshteinDistance(a: string, b: string): number {
  const m = a.length;
  const n = b.length;
  if (m === 0) return n;
  if (n === 0) return m;

  const dp: number[][] = Array(m + 1)
    .fill(null)
    .map(() => Array(n + 1).fill(0));

  for (let i = 0; i <= m; i++) dp[i][0] = i;
  for (let j = 0; j <= n; j++) dp[0][j] = j;

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      dp[i][j] = Math.min(
        dp[i - 1][j] + 1,
        dp[i][j - 1] + 1,
        dp[i - 1][j - 1] + cost
      );
    }
  }
  return dp[m][n];
}

export interface SuggestionWithDistance {
  name: string;
  distance: number;
}

export function suggestAlternativesWithDistance(
  query: string,
  candidates: string[],
  maxDistance = 3
): SuggestionWithDistance[] {
  return candidates
    .map((c) => ({ name: c, distance: levenshteinDistance(query, c) }))
    .filter(({ distance }) => distance <= maxDistance && distance > 0)
    .sort((a, b) => a.distance - b.distance);
}

export function suggestAlternatives(
  query: string,
  candidates: string[],
  maxDistance = 3
): string[] {
  return suggestAlternativesWithDistance(query, candidates, maxDistance).map(({ name }) => name);
}
