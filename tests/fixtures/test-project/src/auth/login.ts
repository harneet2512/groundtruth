export interface LoginResult {
  token: string;
  refreshToken: string;
  expiresAt: Date;
}
export async function login(email: string, password: string): Promise<LoginResult> {
  return { token: '', refreshToken: '', expiresAt: new Date() };
}
