package middleware

import (
	"fmt"
	"strings"

	"example.com/project/auth"
)

// AuthMiddleware validates the Authorization header and attaches the token
// payload to the request context. It returns the decoded TokenPayload and
// an error if authentication fails.
func AuthMiddleware(authHeader string) (*auth.TokenPayload, error) {
	if authHeader == "" {
		return nil, fmt.Errorf("missing authorization header")
	}

	parts := strings.SplitN(authHeader, " ", 2)
	if len(parts) != 2 || strings.ToLower(parts[0]) != "bearer" {
		return nil, fmt.Errorf("invalid authorization header format, expected 'Bearer <token>'")
	}

	token := parts[1]
	payload, err := auth.VerifyToken(token)
	if err != nil {
		return nil, fmt.Errorf("authentication failed: %w", err)
	}

	return payload, nil
}
