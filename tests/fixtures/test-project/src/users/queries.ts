import { User, CreateUserInput, UpdateUserInput } from './types.js';
export async function getUserById(id: number): Promise<User | null> {
  return null;
}
export async function createUser(data: CreateUserInput): Promise<User> {
  return {} as User;
}
export async function updateUser(id: number, data: UpdateUserInput): Promise<User> {
  return {} as User;
}
export async function deleteUser(id: number): Promise<void> {}
