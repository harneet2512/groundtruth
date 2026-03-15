package auth

import "fmt"

// VerifyToken validates the given token string and returns its decoded payload.
func VerifyToken(token string) (*TokenPayload, error) {
	if token == "" {
		return nil, fmt.Errorf("token cannot be empty")
	}

	payload, err := DecodeToken(token)
	if err != nil {
		return nil, fmt.Errorf("token verification failed: %w", err)
	}

	return payload, nil
}
