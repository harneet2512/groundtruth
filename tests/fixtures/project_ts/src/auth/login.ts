/**
 * Login functionality.
 */

import { getUserByEmail } from "../users/queries";
import { comparePassword } from "../utils/crypto";
import { AuthenticationError } from "../utils/errors";
import { signToken, TokenPayload } from "./jwt";

export interface LoginResult {
  token: string;
  user: {
    id: number;
    email: string;
    name: string;
  };
}

export async function login(email: string, password: string): Promise<LoginResult> {
  const user = await getUserByEmail(email);

  if (!user) {
    throw new AuthenticationError("Invalid email or password");
  }

  const isValid = await comparePassword(password, user.passwordHash);

  if (!isValid) {
    throw new AuthenticationError("Invalid email or password");
  }

  const payload: TokenPayload = { userId: user.id, email: user.email };
  const token = signToken(payload);

  return {
    token,
    user: {
      id: user.id,
      email: user.email,
      name: user.name,
    },
  };
}
