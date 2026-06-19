from typing import Optional


class User:
    def __init__(self, uid: int, name: str) -> None:
        self.uid = uid
        self.name = name


def get_user(user_id: int) -> Optional[User]:
    """Return a user or None. Raises on non-positive id."""
    if user_id <= 0:
        raise ValueError("user_id must be positive")
    if user_id == 1:
        return User(1, "root")
    return None


def isolated_helper() -> None:
    """No caller anywhere — tests Contract-pillar always-fire on a 0-caller function."""
    return None


def added_fn():
    return get_user(1)
