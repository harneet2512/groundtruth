package server

import "fmt"

// RunServer is the CLI entry point — wires Config, Logger, and routes.
func RunServer() {
	cfg := LoadConfig()
	logger := NewLogger(cfg.LogLevel)
	logger.Log("starting", map[string]any{"port": cfg.Port})
	RegisterRoutes()
	fmt.Printf("listening on :%d\n", cfg.Port)
}
