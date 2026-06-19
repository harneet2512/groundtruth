export interface TokenPayload {
  userId: string;
  email: string;
  iat: number;
}
export async function verifyToken(token: string): Promise<TokenPayload> {
  return { userId: '', email: '', iat: 0 };
}
