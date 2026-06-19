import { TokenPayload } from './verify.js';
export function signToken(payload: { userId: string; email: string }): string {
  return '';
}
export function decodeToken(token: string): TokenPayload {
  return { userId: '', email: '', iat: 0 };
}
