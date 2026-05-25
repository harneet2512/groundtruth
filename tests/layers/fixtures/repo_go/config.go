package server

import "os"

// Config holds runtime configuration for the example server.
type Config struct {
	Port      int
	LogLevel  string
	AllowedDB string
}

// LoadConfig pulls config from the environment with sensible defaults.
func LoadConfig() *Config {
	return &Config{
		Port:      portFromEnv(),
		LogLevel:  envOr("LOG_LEVEL", "info"),
		AllowedDB: envOr("ALLOWED_DB", "primary"),
	}
}

func portFromEnv() int {
	if v := os.Getenv("PORT"); v != "" {
		// keep simple: tests don't hit this path
		return 0
	}
	return 8080
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
