/**
 * Test fixture: Auth module.
 * The model commonly hallucinates "authenticate()" but this project
 * uses "login()" — this is the canonical test case for GroundTruth.
 */

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface JWTPayload {
  userId: string;
  email: string;
  role: string;
  exp: number;
}

export async function login(credentials: LoginCredentials): Promise<JWTPayload> {
  // Fixture implementation
  return {
    userId: "123",
    email: credentials.email,
    role: "user",
    exp: Date.now() + 3600000,
  };
}

export async function verifyToken(token: string): Promise<JWTPayload> {
  // Fixture implementation
  return {
    userId: "123",
    email: "test@example.com",
    role: "user",
    exp: Date.now() + 3600000,
  };
}

export async function logout(): Promise<void> {
  // Fixture implementation
}
