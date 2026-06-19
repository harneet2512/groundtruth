from app.models import get_user


def handle(uid: int) -> str:
    user = get_user(uid)
    if not user:
        return "missing"
    return user.name
