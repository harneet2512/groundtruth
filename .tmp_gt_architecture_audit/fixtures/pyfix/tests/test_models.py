from app.models import get_user


def test_get_user_missing():
    assert get_user(2) is None


def test_get_user_root():
    assert get_user(1).name == "root"
