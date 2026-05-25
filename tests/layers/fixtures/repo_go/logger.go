package server

import (
	"encoding/json"
	"os"
	"time"
)

// Logger is the structured logger used across the package.
type Logger struct {
	Level string
}

// NewLogger constructs a Logger.
func NewLogger(level string) *Logger {
	return &Logger{Level: level}
}

// Log emits a single structured event to stdout.
func (l *Logger) Log(msg string, fields map[string]any) {
	rec := map[string]any{
		"level": l.Level,
		"msg":   msg,
		"ts":    time.Now().Unix(),
	}
	for k, v := range fields {
		rec[k] = v
	}
	enc := json.NewEncoder(os.Stdout)
	_ = enc.Encode(rec)
}
