import { signToken, verifyToken } from '../jwt';

describe('signToken', () => {
  it('should return a valid JWT string', () => {
    const token = signToken({ userId: 1, email: 'test@example.com' });
    expect(token).toBeDefined();
    expect(token.split('.')).toHaveLength(3);
  });

  it('should throw on empty payload', () => {
    expect(() => signToken(null as any)).toThrow();
  });
});

describe('verifyToken', () => {
  it('should decode a valid token', () => {
    const token = signToken({ userId: 42 });
    const decoded = verifyToken(token);
    expect(decoded.userId).toBe(42);
  });

  it('should throw on invalid token', () => {
    expect(() => verifyToken('invalid-token')).toThrow();
  });

  it('should reject expired tokens', () => {
    const token = signToken({ userId: 1, exp: 0 });
    expect(() => verifyToken(token)).toThrow('expired');
  });
});
