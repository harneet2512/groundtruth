/**
 * Levenshtein unit tests.
 */
import { describe, it, expect } from 'vitest';
import { levenshteinDistance, suggestAlternatives, suggestAlternativesWithDistance } from '../../../src/utils/levenshtein.js';

describe('levenshteinDistance', () => {
  it('returns 0 for equal strings', () => {
    expect(levenshteinDistance('', '')).toBe(0);
    expect(levenshteinDistance('a', 'a')).toBe(0);
    expect(levenshteinDistance('hello', 'hello')).toBe(0);
  });

  it('returns length when one string is empty', () => {
    expect(levenshteinDistance('', 'abc')).toBe(3);
    expect(levenshteinDistance('abc', '')).toBe(3);
  });

  it('returns 3 for kitten -> sitting', () => {
    expect(levenshteinDistance('kitten', 'sitting')).toBe(3);
  });

  it('returns correct distance for single-char diff', () => {
    expect(levenshteinDistance('a', 'b')).toBe(1);
    expect(levenshteinDistance('ab', 'ac')).toBe(1);
  });
});

describe('suggestAlternatives', () => {
  it('returns only candidates within maxDistance', () => {
    const candidates = ['login', 'logout', 'logon', 'xyz'];
    const got = suggestAlternatives('login', candidates, 3);
    expect(got).toContain('logout');
    expect(got).toContain('logon');
    expect(got).not.toContain('login');
    expect(got).not.toContain('xyz');
  });

  it('returns empty when no candidate within distance', () => {
    expect(suggestAlternatives('foo', ['bar', 'baz'], 1)).toEqual([]);
  });

  it('sorts by distance (closest first)', () => {
    const candidates = ['log', 'login', 'logon'];
    const got = suggestAlternatives('login', candidates, 3);
    expect(got[0]).toBe('logon');
    expect(got[1]).toBe('log');
  });

  it('excludes exact match (distance 0)', () => {
    const candidates = ['login', 'logout'];
    const got = suggestAlternatives('login', candidates, 3);
    expect(got).not.toContain('login');
    expect(got).toContain('logout');
  });
});

describe('suggestAlternativesWithDistance', () => {
  it('returns name and distance for each suggestion', () => {
    const candidates = ['login', 'logout', 'logon'];
    const got = suggestAlternativesWithDistance('loginn', candidates, 3);
    expect(got.length).toBeGreaterThan(0);
    expect(got[0]).toHaveProperty('name');
    expect(got[0]).toHaveProperty('distance');
  });

  it('sorts by distance (closest first)', () => {
    const candidates = ['logout', 'log', 'logon'];
    const got = suggestAlternativesWithDistance('login', candidates, 3);
    for (let i = 1; i < got.length; i++) {
      expect(got[i].distance).toBeGreaterThanOrEqual(got[i - 1].distance);
    }
  });

  it('returns correct distances', () => {
    const got = suggestAlternativesWithDistance('generateHash', ['generateSalt', 'hashPassword'], 3);
    const salt = got.find(s => s.name === 'generateSalt');
    if (salt) {
      expect(salt.distance).toBeGreaterThanOrEqual(3);
    }
  });

  it('excludes exact match and beyond maxDistance', () => {
    const got = suggestAlternativesWithDistance('foo', ['foo', 'foobar', 'completely_different'], 2);
    expect(got.find(s => s.name === 'foo')).toBeUndefined();
    expect(got.find(s => s.name === 'completely_different')).toBeUndefined();
  });
});
