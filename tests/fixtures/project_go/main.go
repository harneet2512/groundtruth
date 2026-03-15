package main

import (
	"fmt"
	"os"

	"example.com/project/auth"
	"example.com/project/db"
	"example.com/project/middleware"
	"example.com/project/users"
	"example.com/project/utils"
)

func main() {
	// Initialize the database client.
	database := db.NewDB("postgres://localhost:5432/project")
	if err := database.Connect(); err != nil {
		fmt.Fprintf(os.Stderr, "failed to connect to database: %v\n", err)
		os.Exit(1)
	}
	defer database.Close()

	// Create a user repository.
	userRepo := users.NewRepository(database)

	// Demonstrate user creation.
	newUser, err := userRepo.CreateUser(users.CreateUserInput{
		Email:    "alice@example.com",
		Name:     "Alice",
		Password: "securepassword123",
	})
	if err != nil {
		resp := middleware.ErrorHandler(err)
		fmt.Fprintf(os.Stderr, "error creating user: [%d] %s\n", resp.Code, resp.Message)
		os.Exit(1)
	}
	fmt.Printf("Created user: %s (ID: %d)\n", newUser.Name, newUser.ID)

	// Demonstrate login.
	loginResult, err := auth.Login(newUser.Email, "securepassword123")
	if err != nil {
		fmt.Fprintf(os.Stderr, "login failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Login token: %s\n", loginResult.Token[:20]+"...")

	// Demonstrate auth middleware.
	payload, err := middleware.AuthMiddleware("Bearer " + loginResult.Token)
	if err != nil {
		fmt.Fprintf(os.Stderr, "auth middleware failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Authenticated user ID: %d\n", payload.UserID)

	// Demonstrate user lookup.
	user, err := userRepo.GetUserByID(1)
	if err != nil {
		resp := middleware.ErrorHandler(err)
		fmt.Fprintf(os.Stderr, "error fetching user: [%d] %s\n", resp.Code, resp.Message)
		os.Exit(1)
	}
	fmt.Printf("Found user: %s (%s)\n", user.Name, user.Email)

	// Demonstrate logout.
	if err := auth.Logout(loginResult.Token); err != nil {
		fmt.Fprintf(os.Stderr, "logout failed: %v\n", err)
	}

	// Demonstrate utility functions.
	fmt.Printf("Email valid: %v\n", utils.ValidateEmail("test@example.com"))
	fmt.Printf("Sanitized: %q\n", utils.SanitizeInput("  hello\x00world  "))

	appErr := utils.NewAppError(503, "service unavailable")
	resp := middleware.ErrorHandler(appErr)
	fmt.Printf("Error handled: [%d] %s\n", resp.Code, resp.Message)
}
