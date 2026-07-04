export async function hashPassword(password: string): Promise<string> {
  return '';
}
export async function comparePassword(plain: string, hashed: string): Promise<boolean> {
  return false;
}
export function generateSalt(rounds?: number): string {
  return '';
}
