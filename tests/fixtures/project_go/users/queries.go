package users

import (
	"fmt"
	"time"

	"example.com/project/db"
	"example.com/project/utils"
)

// Repository provides user data access methods.
type Repository struct {
	DB *db.DB
}

// NewRepository creates a new user repository with the given database client.
func NewRepository(database *db.DB) *Repository {
	return &Repository{DB: database}
}

// GetUserByID retrieves a user by their ID. Returns NotFoundError if not found.
func (r *Repository) GetUserByID(id int) (*User, error) {
	rows, err := r.DB.Query("SELECT * FROM users WHERE id = ?", id)
	if err != nil {
		return nil, fmt.Errorf("failed to query user: %w", err)
	}
	if len(rows) == 0 {
		return nil, utils.NewNotFoundError("User", fmt.Sprintf("%d", id))
	}
	user := &User{
		ID:        id,
		Email:     "stub@example.com",
		Name:      "Stub User",
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}
	return user, nil
}

// CreateUser creates a new user from the given input.
func (r *Repository) CreateUser(input CreateUserInput) (*User, error) {
	if !utils.ValidateEmail(input.Email) {
		return nil, utils.NewValidationError("email", "invalid email address")
	}
	if !utils.ValidatePassword(input.Password) {
		return nil, utils.NewValidationError("password", "password must be at least 8 characters")
	}

	salt, err := utils.GenerateSalt(16)
	if err != nil {
		return nil, fmt.Errorf("failed to generate salt: %w", err)
	}

	hashed, err := utils.HashPassword(input.Password, salt)
	if err != nil {
		return nil, fmt.Errorf("failed to hash password: %w", err)
	}

	err = r.DB.Exec("INSERT INTO users (email, name, password, salt) VALUES (?, ?, ?, ?)",
		input.Email, input.Name, hashed, salt)
	if err != nil {
		return nil, fmt.Errorf("failed to insert user: %w", err)
	}

	user := &User{
		ID:        1,
		Email:     input.Email,
		Name:      input.Name,
		Password:  hashed,
		Salt:      salt,
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}
	return user, nil
}

// UpdateUser updates an existing user by ID with the given input fields.
func (r *Repository) UpdateUser(id int, input UpdateUserInput) (*User, error) {
	existing, err := r.GetUserByID(id)
	if err != nil {
		return nil, err
	}

	if input.Email != nil {
		if !utils.ValidateEmail(*input.Email) {
			return nil, utils.NewValidationError("email", "invalid email address")
		}
		existing.Email = *input.Email
	}
	if input.Name != nil {
		existing.Name = *input.Name
	}

	existing.UpdatedAt = time.Now()
	return existing, nil
}

// DeleteUser deletes a user by ID. Returns NotFoundError if the user does not exist.
func (r *Repository) DeleteUser(id int) error {
	_, err := r.GetUserByID(id)
	if err != nil {
		return err
	}

	err = r.DB.Exec("DELETE FROM users WHERE id = ?", id)
	if err != nil {
		return fmt.Errorf("failed to delete user: %w", err)
	}
	return nil
}
