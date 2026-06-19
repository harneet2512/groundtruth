package auth

import (
	"fmt"

	"example.com/project/utils"
)

// LoginResult holds the result of a successful login attempt.
type LoginResult struct {
	Token   string        `json:"token"`
	Payload *TokenPayload `json:"payload"`
}

// Login authenticates a user with the given email and password.
// Returns a LoginResult containing a signed JWT token on success.
func Login(email, password string) (*LoginResult, error) {
	email = utils.SanitizeInput(email)
	if !utils.ValidateEmail(email) {
		return nil, utils.NewValidationError("email", "invalid email address")
	}
	if !utils.ValidatePassword(password) {
		return nil, utils.NewValidationError("password", "password must be at least 8 characters")
	}

	// Stub: In a real implementation, look up the user and verify the password.
	// For now, generate a token with stub data.
	payload := map[string]interface{}{
		"user_id": 1,
		"email":   email,
	}

	token, err := SignToken(payload)
	if err != nil {
		return nil, fmt.Errorf("failed to sign token: %w", err)
	}

	tokenPayload, err := DecodeToken(token)
	if err != nil {
		return nil, fmt.Errorf("failed to decode token: %w", err)
	}

	return &LoginResult{
		Token:   token,
		Payload: tokenPayload,
	}, nil
}
