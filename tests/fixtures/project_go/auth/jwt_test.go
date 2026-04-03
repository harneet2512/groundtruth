package auth

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestSignToken(t *testing.T) {
	payload := map[string]interface{}{"user_id": 1, "email": "test@example.com"}
	token, err := SignToken(payload)
	require.NoError(t, err)
	assert.NotEmpty(t, token)
	assert.Contains(t, token, ".")
}

func TestSignTokenNilPayload(t *testing.T) {
	_, err := SignToken(nil)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "nil")
}

func TestVerifyToken(t *testing.T) {
	payload := map[string]interface{}{"user_id": 42}
	token, err := SignToken(payload)
	require.NoError(t, err)

	decoded, err := VerifyToken(token)
	require.NoError(t, err)
	assert.Equal(t, 42, decoded.UserID)
}

func TestVerifyTokenInvalid(t *testing.T) {
	_, err := VerifyToken("invalid-token")
	assert.Error(t, err)
}
