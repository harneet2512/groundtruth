package resolver

import "testing"

func TestNormalizePath(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		// Basic paths
		{"/api/users", "/api/users"},
		{"/api/users/", "/api/users"},
		// Strip path parameters
		{"/api/users/{id}", "/api/users"},
		{"/api/users/:id", "/api/users"},
		{"/api/users/<user_id>", "/api/users"},
		// Strip query strings
		{"/api/users?page=1", "/api/users"},
		// Strip full URL prefix
		{"http://auth-service/api/validate", "/api/validate"},
		{"https://localhost:8080/v1/tokens", "/v1/tokens"},
		// Numeric path values treated as params
		{"/api/users/123/posts", "/api/users/posts"},
		// UUID treated as param
		{"/api/items/550e8400-e29b-41d4-a716-446655440000/detail", "/api/items/detail"},
		// Root only
		{"http://svc/", "/"},
		// Complex
		{"http://user-service:3000/api/v2/users/{userId}/roles?active=true", "/api/v2/users/roles"},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := normalizePath(tt.input)
			if got != tt.want {
				t.Errorf("normalizePath(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestExtractMethod(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"get", "GET"},
		{"Get", "GET"},
		{"post", "POST"},
		{"Post", "POST"},
		{"put", "PUT"},
		{"delete", "DELETE"},
		{"patch", "PATCH"},
		{"route", ""},
		{"HandleFunc", ""},
		{"Handle", ""},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := extractMethod(tt.input)
			if got != tt.want {
				t.Errorf("extractMethod(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestRoutePatternMatching(t *testing.T) {
	tests := []struct {
		name    string
		line    string
		wantHit bool
	}{
		// Python Flask/FastAPI
		{"flask route", `@app.route("/api/users")`, true},
		{"fastapi get", `@router.get("/api/users/{id}")`, true},
		{"fastapi post", `@app.post("/api/orders")`, true},
		// Go
		{"go handlefunc", `r.HandleFunc("/api/users", handleUsers)`, true},
		{"go mux handle", `mux.Handle("/api/v1/users", handler)`, true},
		// Express.js
		{"express get", `app.get("/api/users", getUsers)`, true},
		{"express router", `router.post("/api/users", createUser)`, true},
		// Non-matches
		{"plain string", `path = "/api/users"`, false},
		{"comment", `// app.get("/old")`, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			matched := false
			for _, re := range routePatterns {
				if re.FindStringSubmatch(tt.line) != nil {
					matched = true
					break
				}
			}
			if matched != tt.wantHit {
				t.Errorf("route pattern match for %q: got %v, want %v", tt.line, matched, tt.wantHit)
			}
		})
	}
}

func TestClientPatternMatching(t *testing.T) {
	tests := []struct {
		name    string
		line    string
		wantHit bool
	}{
		// Python
		{"requests get", `resp = requests.get("http://auth/api/validate")`, true},
		{"httpx post", `r = httpx.post("http://svc/api/users")`, true},
		// JS/TS fetch
		{"fetch", `const res = await fetch("/api/users")`, true},
		// axios
		{"axios get", `axios.get("/api/orders")`, true},
		// Go
		{"go http get", `resp, err := http.Get("http://svc/api/health")`, true},
		// Non-matches
		{"variable", `url = "/api/users"`, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			matched := false
			for _, re := range clientPatterns {
				if re.FindStringSubmatch(tt.line) != nil {
					matched = true
					break
				}
			}
			if matched != tt.wantHit {
				t.Errorf("client pattern match for %q: got %v, want %v", tt.line, matched, tt.wantHit)
			}
		})
	}
}
