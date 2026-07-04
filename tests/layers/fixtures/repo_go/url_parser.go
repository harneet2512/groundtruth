package server

import (
	"errors"
	"net/url"
	"strings"
)

// ParsedRequest is the structured form of a parsed inbound URL.
type ParsedRequest struct {
	Scheme string
	Host   string
	Path   string
}

// ParseRequestURL parses and validates a request URL string. The httpHandler
// uses this on every inbound request.
func ParseRequestURL(raw string) (*ParsedRequest, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, errors.New("ParseRequestURL: empty input")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, err
	}
	if u.Host == "" {
		return nil, errors.New("ParseRequestURL: missing host")
	}
	return &ParsedRequest{Scheme: u.Scheme, Host: u.Host, Path: u.Path}, nil
}

// NormalizeHost lower-cases the host portion of a parsed request.
func NormalizeHost(p *ParsedRequest) string {
	return strings.ToLower(p.Host)
}
