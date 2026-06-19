package utils

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// HashPassword hashes a password with the given salt using SHA-256.
func HashPassword(password, salt string) (string, error) {
	if password == "" {
		return "", fmt.Errorf("password cannot be empty")
	}
	h := sha256.New()
	h.Write([]byte(password + salt))
	return hex.EncodeToString(h.Sum(nil)), nil
}

// ComparePassword compares a plaintext password against a hashed password.
func ComparePassword(password, salt, hashed string) (bool, error) {
	computed, err := HashPassword(password, salt)
	if err != nil {
		return false, err
	}
	return computed == hashed, nil
}

// GenerateSalt generates a random salt string of the specified byte length.
func GenerateSalt(length int) (string, error) {
	bytes := make([]byte, length)
	_, err := rand.Read(bytes)
	if err != nil {
		return "", fmt.Errorf("failed to generate salt: %w", err)
	}
	return hex.EncodeToString(bytes), nil
}
