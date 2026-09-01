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
		// P2-10: concrete numeric/uuid segments are KEPT in the key (precision —
		// distinct concrete routes must not collapse to one match key). Only DECLARED
		// params ({}/:/<>) are dropped.
		{"/api/users/123/posts", "/api/users/123/posts"},
		{"/api/items/550e8400-e29b-41d4-a716-446655440000/detail", "/api/items/550e8400-e29b-41d4-a716-446655440000/detail"},
		// Root only
		{"http://svc/", "/"},
		// Complex: the {userId} DECLARED param is still dropped; concrete v2 kept.
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

// TestNormalizePathKeepsConcreteSegments is the BITING test for P2-10: two DISTINCT
// concrete routes that differ only in a numeric/uuid segment must NOT normalize to the
// same match key (which previously minted a false API_CALL between unrelated endpoints).
func TestNormalizePathKeepsConcreteSegments(t *testing.T) {
	a := normalizePath("/orders/42/items")
	b := normalizePath("/orders/99/items")
	if a == b {
		t.Errorf("distinct concrete routes collapsed to the same key %q — false API_CALL risk (numeric segment dropped)", a)
	}
	// The uuid case likewise stays distinct.
	u1 := normalizePath("/tenant/550e8400-e29b-41d4-a716-446655440000/config")
	u2 := normalizePath("/tenant/6ba7b810-9dad-11d1-80b4-00c04fd430c8/config")
	if u1 == u2 {
		t.Errorf("distinct uuid routes collapsed to the same key %q — false API_CALL risk", u1)
	}
}

// TestAPIRouteConfidenceScalesWithAmbiguity pins that the API_CALL confidence falls as
// the number of matched routes rises (P2-10): a unique match is verified-tier (0.7), an
// ambiguous match (>1 route sharing the path) is demoted below it.
func TestAPIRouteConfidenceScalesWithAmbiguity(t *testing.T) {
	cases := []struct {
		matched int
		want    float64
	}{{1, 0.7}, {2, 0.5}, {3, 0.4}, {5, 0.4}, {6, 0.2}}
	for _, c := range cases {
		if got := apiRouteConfidence(c.matched); got != c.want {
			t.Errorf("apiRouteConfidence(%d) = %v, want %v", c.matched, got, c.want)
		}
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

func TestFrameworkManifestIsClosedToCoordinatorFive(t *testing.T) {
	want := []string{"Python", "TypeScript", "JavaScript", "Go", "Java"}
	if len(FrameworkManifest) != len(want) {
		t.Fatalf("manifest has %d languages, want %d", len(FrameworkManifest), len(want))
	}
	for i, language := range want {
		if FrameworkManifest[i].Language != language {
			t.Errorf("manifest[%d] language=%q, want %q", i, FrameworkManifest[i].Language, language)
		}
		if len(FrameworkManifest[i].Mechanisms) == 0 || FrameworkManifest[i].Framework == "" {
			t.Errorf("manifest[%d] missing framework mechanisms", i)
		}
	}
}

func TestExtractFrameworkRoutesCarriesMechanismIdentity(t *testing.T) {
	tests := []struct {
		name, line, language, framework, mechanism, path, method string
	}{
		{"spring", `@GetMapping("/api/users")`, "Java", "Spring", "request_mapping", "/api/users", ""},
		{"gin", `r.GET("/api/users", handler)`, "Go", "gin/echo", "route_registration", "/api/users", "GET"},
		{"next", `export async function GET(request)`, "TypeScript", "Next.js", "app_router_handler", "/", "GET"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			routes := ExtractFrameworkRoutes(test.line)
			if len(routes) != 1 {
				t.Fatalf("got %d routes, want 1", len(routes))
			}
			got := routes[0]
			if got.Language != test.language || got.Framework != test.framework || got.Mechanism != test.mechanism || got.Path != test.path || got.Method != test.method {
				t.Fatalf("route=%+v, want language=%q framework=%q mechanism=%q path=%q method=%q", got, test.language, test.framework, test.mechanism, test.path, test.method)
			}
		})
	}
}
