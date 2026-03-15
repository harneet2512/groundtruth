/**
 * Cryptographic utility functions for password hashing and comparison.
 */

import * as bcrypt from "bcrypt";

const DEFAULT_SALT_ROUNDS = 12;

export function generateSalt(rounds: number = DEFAULT_SALT_ROUNDS): Promise<string> {
  return bcrypt.genSalt(rounds);
}

export async function hashPassword(password: string, saltRounds?: number): Promise<string> {
  const salt = await generateSalt(saltRounds);
  const hashed = await bcrypt.hash(password, salt);
  return hashed;
}

export async function comparePassword(
  plainPassword: string,
  hashedPassword: string
): Promise<boolean> {
  const isMatch = await bcrypt.compare(plainPassword, hashedPassword);
  return isMatch;
}

export function generateRandomToken(length: number = 32): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let result = "";
  for (let i = 0; i < length; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}
