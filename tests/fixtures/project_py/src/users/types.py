from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class User(BaseModel):
    """Core user model returned from database queries."""

    id: int
    email: str
    name: str
    hashed_password: str
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None


class CreateUserInput(BaseModel):
    """Input model for creating a new user."""

    email: str
    name: str
    password: str = Field(min_length=8)


class UpdateUserInput(BaseModel):
    """Input model for updating an existing user. All fields optional."""

    email: str | None = None
    name: str | None = None
    password: str | None = Field(default=None, min_length=8)
    is_active: bool | None = None
