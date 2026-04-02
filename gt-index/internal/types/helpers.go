package types

import "strings"

// LastDotComponent returns the part after the last dot: "foo.bar.baz" → "baz".
func LastDotComponent(s string) string {
	if idx := strings.LastIndex(s, "."); idx >= 0 {
		return s[idx+1:]
	}
	return s
}

// LastSlashComponent returns the part after the last slash: "foo/bar/baz" → "baz".
func LastSlashComponent(s string) string {
	if idx := strings.LastIndex(s, "/"); idx >= 0 {
		return s[idx+1:]
	}
	return s
}

// LastColonComponent returns the part after the last "::": "foo::bar" → "bar".
func LastColonComponent(s string) string {
	if idx := strings.LastIndex(s, "::"); idx >= 0 {
		return s[idx+2:]
	}
	return s
}

// StripQuotes removes surrounding quotes (single, double, or backtick).
func StripQuotes(s string) string {
	if len(s) >= 2 {
		if (s[0] == '"' && s[len(s)-1] == '"') ||
			(s[0] == '\'' && s[len(s)-1] == '\'') ||
			(s[0] == '`' && s[len(s)-1] == '`') {
			return s[1 : len(s)-1]
		}
	}
	return s
}
