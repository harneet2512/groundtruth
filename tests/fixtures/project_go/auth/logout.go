package auth

import "fmt"

// Logout invalidates the given token, ending the user's session.
func Logout(token string) error {
	if token == "" {
		return fmt.Errorf("token cannot be empty")
	}

	// Verify the token is valid before attempting to invalidate it.
	_, err := DecodeToken(token)
	if err != nil {
		return fmt.Errorf("cannot logout with invalid token: %w", err)
	}

	// Stub: In a real implementation, add the token to a blocklist or
	// remove it from a session store.
	return nil
}
