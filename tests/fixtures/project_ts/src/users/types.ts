/**
 * User-related type definitions.
 */

export interface User {
  id: number;
  email: string;
  name: string;
  passwordHash: string;
  createdAt: Date;
  updatedAt: Date;
  isActive: boolean;
}

export interface CreateUserInput {
  email: string;
  name: string;
  password: string;
}

export interface UpdateUserInput {
  email?: string;
  name?: string;
  password?: string;
  isActive?: boolean;
}

export interface UserPublic {
  id: number;
  email: string;
  name: string;
  createdAt: Date;
  isActive: boolean;
}

export function toPublicUser(user: User): UserPublic {
  return {
    id: user.id,
    email: user.email,
    name: user.name,
    createdAt: user.createdAt,
    isActive: user.isActive,
  };
}
