package middleware

import (
	"errors"

	"example.com/project/utils"
)

// ErrorResponse is the structured error response returned to clients.
type ErrorResponse struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// ErrorHandler translates application errors into structured ErrorResponse values.
// It recognizes AppError, NotFoundError, and ValidationError types.
func ErrorHandler(err error) *ErrorResponse {
	if err == nil {
		return nil
	}

	var appErr *utils.AppError
	if errors.As(err, &appErr) {
		return &ErrorResponse{
			Code:    appErr.Code,
			Message: appErr.Message,
		}
	}

	var notFoundErr *utils.NotFoundError
	if errors.As(err, &notFoundErr) {
		return &ErrorResponse{
			Code:    404,
			Message: notFoundErr.Error(),
		}
	}

	var validationErr *utils.ValidationError
	if errors.As(err, &validationErr) {
		return &ErrorResponse{
			Code:    400,
			Message: validationErr.Error(),
		}
	}

	// Unknown error — return generic 500.
	return &ErrorResponse{
		Code:    500,
		Message: "internal server error",
	}
}
