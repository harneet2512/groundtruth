package utils

import (
	"regexp"
	"strings"
)

var emailRegex = regexp.MustCompile(`^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$`)

// ValidateEmail checks whether the given string is a valid email address.
func ValidateEmail(email string) bool {
	return emailRegex.MatchString(email)
}

// ValidatePassword checks whether the password meets minimum requirements.
// Returns true if the password is at least 8 characters long.
func ValidatePassword(password string) bool {
	return len(password) >= 8
}

// SanitizeInput trims whitespace and removes null bytes from the input string.
func SanitizeInput(input string) string {
	sanitized := strings.TrimSpace(input)
	sanitized = strings.ReplaceAll(sanitized, "\x00", "")
	return sanitized
}
