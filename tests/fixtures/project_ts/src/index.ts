/**
 * Application entry point.
 */

import express from "express";
import { db } from "./db/client";
import { authMiddleware } from "./middleware/auth";
import { errorHandler, notFoundHandler } from "./middleware/errorHandler";
import { login } from "./auth";
import { getUserById, createUser } from "./users";
import { validateEmail, validatePassword } from "./utils/validation";
import { ValidationError } from "./utils/errors";

const app = express();
const PORT = parseInt(process.env.PORT || "3000", 10);

app.use(express.json());

// Public routes
app.post("/api/login", async (req, res, next) => {
  try {
    const { email, password } = req.body;
    const result = await login(email, password);
    res.json({ success: true, data: result });
  } catch (error) {
    next(error);
  }
});

app.post("/api/register", async (req, res, next) => {
  try {
    const { email, name, password } = req.body;

    if (!validateEmail(email)) {
      throw new ValidationError("Invalid email", { email: "Must be a valid email address" });
    }

    const passwordCheck = validatePassword(password);
    if (!passwordCheck.valid) {
      throw new ValidationError("Invalid password", { password: passwordCheck.errors.join(", ") });
    }

    const user = await createUser({ email, name, password });
    res.status(201).json({ success: true, data: { id: user.id, email: user.email, name: user.name } });
  } catch (error) {
    next(error);
  }
});

// Protected routes
app.get("/api/users/:id", authMiddleware, async (req, res, next) => {
  try {
    const user = await getUserById(parseInt(req.params.id, 10));
    res.json({ success: true, data: { id: user.id, email: user.email, name: user.name } });
  } catch (error) {
    next(error);
  }
});

// Error handling
app.use(notFoundHandler);
app.use(errorHandler);

async function start(): Promise<void> {
  await db.connect();
  app.listen(PORT, () => {
    console.log(`Server running on port ${PORT}`);
  });
}

start().catch(console.error);

export { app };
