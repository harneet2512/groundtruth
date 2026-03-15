export interface User {
  id: number;
  email: string;
  name: string;
  passwordHash: string;
  createdAt: Date;
}
export interface CreateUserInput {
  email: string;
  name: string;
  password: string;
}
export interface UpdateUserInput {
  name?: string;
  email?: string;
}
