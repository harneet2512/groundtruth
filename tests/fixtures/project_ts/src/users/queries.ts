/**
 * User database queries.
 */

import { db } from "../db/client";
import { User, CreateUserInput, UpdateUserInput } from "./types";
import { NotFoundError } from "../utils/errors";
import { hashPassword } from "../utils/crypto";

export async function getUserById(userId: number): Promise<User> {
  const result = await db.query<User>("SELECT * FROM users WHERE id = $1", [userId]);
  if (result.rows.length === 0) {
    throw new NotFoundError("User", userId);
  }
  return result.rows[0];
}

export async function getUserByEmail(email: string): Promise<User | null> {
  const result = await db.query<User>("SELECT * FROM users WHERE email = $1", [email]);
  return result.rows.length > 0 ? result.rows[0] : null;
}

export async function createUser(input: CreateUserInput): Promise<User> {
  const passwordHash = await hashPassword(input.password);
  const result = await db.query<User>(
    "INSERT INTO users (email, name, password_hash, created_at, updated_at, is_active) VALUES ($1, $2, $3, NOW(), NOW(), true) RETURNING *",
    [input.email, input.name, passwordHash]
  );
  return result.rows[0];
}

export async function updateUser(userId: number, input: UpdateUserInput): Promise<User> {
  const existing = await getUserById(userId);

  const updates: string[] = [];
  const values: unknown[] = [];
  let paramIndex = 1;

  if (input.email !== undefined) {
    updates.push(`email = $${paramIndex++}`);
    values.push(input.email);
  }
  if (input.name !== undefined) {
    updates.push(`name = $${paramIndex++}`);
    values.push(input.name);
  }
  if (input.password !== undefined) {
    const newHash = await hashPassword(input.password);
    updates.push(`password_hash = $${paramIndex++}`);
    values.push(newHash);
  }
  if (input.isActive !== undefined) {
    updates.push(`is_active = $${paramIndex++}`);
    values.push(input.isActive);
  }

  if (updates.length === 0) {
    return existing;
  }

  updates.push(`updated_at = NOW()`);
  values.push(userId);

  const result = await db.query<User>(
    `UPDATE users SET ${updates.join(", ")} WHERE id = $${paramIndex} RETURNING *`,
    values
  );
  return result.rows[0];
}

export async function deleteUser(userId: number): Promise<void> {
  await getUserById(userId); // throws NotFoundError if not exists
  await db.query("DELETE FROM users WHERE id = $1", [userId]);
}

export async function listUsers(page: number = 1, limit: number = 20): Promise<User[]> {
  const offset = (page - 1) * limit;
  const result = await db.query<User>("SELECT * FROM users ORDER BY created_at DESC LIMIT $1 OFFSET $2", [
    limit,
    offset,
  ]);
  return result.rows;
}
