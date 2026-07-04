import pytest
from src.auth.jwt import sign_token, verify_token


class TestSignToken:
    def test_returns_valid_jwt(self):
        token = sign_token({"user_id": 1, "email": "test@example.com"})
        assert isinstance(token, str)
        assert token.count(".") == 2

    def test_none_payload_raises(self):
        with pytest.raises(ValueError):
            sign_token(None)

    def test_empty_payload_raises(self):
        with pytest.raises(ValueError):
            sign_token({})


class TestVerifyToken:
    def test_decode_valid_token(self):
        token = sign_token({"user_id": 42})
        payload = verify_token(token)
        assert payload["user_id"] == 42

    def test_invalid_token_raises(self):
        with pytest.raises(ValueError):
            verify_token("invalid-token")

    def test_expired_token_raises(self):
        token = sign_token({"user_id": 1, "exp": 0})
        with pytest.raises(ValueError, match="expired"):
            verify_token(token)
