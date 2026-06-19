package service

import "errors"

type Runner interface {
	Run(value string) string
}

type UserService struct {
	count int
}

func (s *UserService) Run(value string) string {
	return value
}

func (s *UserService) Validate(value string) (string, error) {
	s.count = s.count + 1
	if value == "" {
		return "", errors.New("missing")
	}
	return s.Run(value), nil
}

func Entry(value string) (string, error) {
	service := &UserService{}
	return service.Validate(value)
}
