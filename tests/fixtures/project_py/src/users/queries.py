from __future__ import annotations

from src.db.client import db
from src.users.types import CreateUserInput, UpdateUserInput, User
from src.utils.crypto import hash_password
from src.utils.errors import NotFoundError


def get_user_by_id(user_id: int) -> User:
    """Fetch a user by their ID. Raises NotFoundError if not found."""
    result = db.query("SELECT * FROM users WHERE id = ?", (user_id,))
    if not result.rows:
        raise NotFoundError(resource="User", identifier=user_id)
    return User(**result.rows[0])


def create_user(data: CreateUserInput) -> User:
    """Create a new user from validated input."""
    hashed, salt = hash_password(data.password)
    db.execute(
        "INSERT INTO users (email, name, hashed_password, salt) VALUES (?, ?, ?, ?)",
        (data.email, data.name, hashed, salt.hex()),
    )
    result = db.query("SELECT * FROM users WHERE email = ?", (data.email,))
    return User(**result.rows[0])


def update_user(user_id: int, data: UpdateUserInput) -> User:
    """Update an existing user. Raises NotFoundError if the user does not exist."""
    get_user_by_id(user_id)  # raises NotFoundError if not found

    updates: dict[str, str | bool | None] = {}
    if data.email is not None:
        updates["email"] = data.email
    if data.name is not None:
        updates["name"] = data.name
    if data.password is not None:
        hashed, _salt = hash_password(data.password)
        updates["hashed_password"] = hashed
    if data.is_active is not None:
        updates["is_active"] = data.is_active

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",
            (*updates.values(), user_id),
        )

    return get_user_by_id(user_id)


def delete_user(user_id: int) -> None:
    """Delete a user by ID. Raises NotFoundError if the user does not exist."""
    _ = get_user_by_id(user_id)  # ensure exists
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
