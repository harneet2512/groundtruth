package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// TokenPayload represents the decoded contents of a JWT token.
type TokenPayload struct {
	UserID    int       `json:"user_id"`
	Email     string    `json:"email"`
	ExpiresAt time.Time `json:"expires_at"`
}

const jwtSecret = "stub-secret-key"

// SignToken creates a signed JWT token from the given payload map.
func SignToken(payload map[string]interface{}) (string, error) {
	if payload == nil {
		return "", fmt.Errorf("payload cannot be nil")
	}

	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	headerJSON, err := json.Marshal(header)
	if err != nil {
		return "", fmt.Errorf("failed to marshal header: %w", err)
	}

	payload["iat"] = time.Now().Unix()
	if _, ok := payload["exp"]; !ok {
		payload["exp"] = time.Now().Add(24 * time.Hour).Unix()
	}

	payloadJSON, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("failed to marshal payload: %w", err)
	}

	headerB64 := base64.RawURLEncoding.EncodeToString(headerJSON)
	payloadB64 := base64.RawURLEncoding.EncodeToString(payloadJSON)
	signingInput := headerB64 + "." + payloadB64

	mac := hmac.New(sha256.New, []byte(jwtSecret))
	mac.Write([]byte(signingInput))
	signature := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))

	return signingInput + "." + signature, nil
}

// DecodeToken decodes and verifies a JWT token string, returning its payload.
func DecodeToken(token string) (*TokenPayload, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, fmt.Errorf("invalid token format")
	}

	signingInput := parts[0] + "." + parts[1]
	mac := hmac.New(sha256.New, []byte(jwtSecret))
	mac.Write([]byte(signingInput))
	expectedSig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))

	if !hmac.Equal([]byte(parts[2]), []byte(expectedSig)) {
		return nil, fmt.Errorf("invalid token signature")
	}

	payloadJSON, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("failed to decode payload: %w", err)
	}

	var raw map[string]interface{}
	if err := json.Unmarshal(payloadJSON, &raw); err != nil {
		return nil, fmt.Errorf("failed to unmarshal payload: %w", err)
	}

	result := &TokenPayload{}
	if uid, ok := raw["user_id"].(float64); ok {
		result.UserID = int(uid)
	}
	if email, ok := raw["email"].(string); ok {
		result.Email = email
	}
	if exp, ok := raw["exp"].(float64); ok {
		result.ExpiresAt = time.Unix(int64(exp), 0)
	}

	if result.ExpiresAt.Before(time.Now()) {
		return nil, fmt.Errorf("token expired")
	}

	return result, nil
}
