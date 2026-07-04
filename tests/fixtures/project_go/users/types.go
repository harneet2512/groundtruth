package users

import "time"

// User represents a user record in the database.
type User struct {
	ID        int       `json:"id"`
	Email     string    `json:"email"`
	Name      string    `json:"name"`
	Password  string    `json:"-"`
	Salt      string    `json:"-"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// CreateUserInput holds the data needed to create a new user.
type CreateUserInput struct {
	Email    string `json:"email"`
	Name     string `json:"name"`
	Password string `json:"password"`
}

// UpdateUserInput holds the data for updating an existing user.
type UpdateUserInput struct {
	Email *string `json:"email,omitempty"`
	Name  *string `json:"name,omitempty"`
}
