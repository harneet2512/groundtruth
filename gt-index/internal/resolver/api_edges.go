package resolver

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// RouteDefinition represents a detected HTTP route handler.
type RouteDefinition struct {
	Path      string // normalized URL path, e.g. "/api/users"
	Method    string // HTTP method (GET, POST, etc.) or "" if unknown
	File      string // relative file path where the route is defined
	Line      int    // line number
	NodeID    int64  // DB node ID of the file (or nearest function)
	Language  string // language owning the framework adapter
	Framework string // framework identity, when detected
	Mechanism string // route/DI/AOP mechanism
}

// FrameworkManifest is the coordinator-minted HAR-70 boundary.  Keep this
// list closed: adding a language requires a new released construction record.
var FrameworkManifest = []struct {
	Language   string
	Framework  string
	Mechanisms []string
}{
	{Language: "Python", Framework: "FastAPI/Flask/Django", Mechanisms: []string{"route_decorator", "Depends"}},
	{Language: "TypeScript", Framework: "Next.js/NestJS/Express", Mechanisms: []string{"app_router_handler", "decorator_di", "route_registration"}},
	{Language: "JavaScript", Framework: "Express/React", Mechanisms: []string{"route_registration", "component_props"}},
	{Language: "Go", Framework: "gin/echo/wire", Mechanisms: []string{"route_registration", "wire_di"}},
	{Language: "Java", Framework: "Spring", Mechanisms: []string{"component_scan", "request_mapping"}},
}

// FrameworkRoute is the producer-owned normalized route fact used by the
// existing API edge path.  It carries mechanism identity so framework edges
// remain distinguishable from heuristic name matches.
type FrameworkRoute struct {
	Language  string
	Framework string
	Mechanism string
	Path      string
	Method    string
}

var frameworkRoutePatterns = []struct {
	re        *regexp.Regexp
	language  string
	framework string
	mechanism string
}{
	{regexp.MustCompile(`^\s*@(?:app|router)\.(?:get|post|put|delete|patch|route)\s*\(\s*["']([^"']+)["']`), "Python", "FastAPI/Flask", "route_decorator"},
	{regexp.MustCompile(`^\s*@(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*["']([^"']+)["']`), "TypeScript", "Express", "route_registration"},
	{regexp.MustCompile(`^\s*export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE)\b`), "TypeScript", "Next.js", "app_router_handler"},
	{regexp.MustCompile(`^\s*@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)\s*\(\s*["']([^"']+)["']`), "Java", "Spring", "request_mapping"},
	{regexp.MustCompile(`^\s*[\w.]+\.(GET|POST|PUT|PATCH|DELETE)\s*\(\s*["']([^"']+)["']`), "Go", "gin/echo", "route_registration"},
}

// ExtractFrameworkRoutes returns only route-bearing framework facts.  DI and
// component-props mechanisms are represented in the manifest but do not mint
// an API_CALL without a concrete route path.
func ExtractFrameworkRoutes(line string) []FrameworkRoute {
	var routes []FrameworkRoute
	for _, pattern := range frameworkRoutePatterns {
		matches := pattern.re.FindStringSubmatch(line)
		if len(matches) == 0 {
			continue
		}
		method, path := "", ""
		if pattern.mechanism == "app_router_handler" {
			method = extractMethod(matches[1])
			path = "/"
		} else if pattern.mechanism == "request_mapping" {
			path = matches[len(matches)-1]
		} else if len(matches) == 2 {
			path = matches[1]
		} else {
			method = extractMethod(matches[1])
			path = matches[2]
		}
		path = normalizePath(path)
		if !isAPIPath(path) && pattern.mechanism != "app_router_handler" {
			continue
		}
		routes = append(routes, FrameworkRoute{Language: pattern.language, Framework: pattern.framework, Mechanism: pattern.mechanism, Path: path, Method: method})
	}
	return routes
}

// ClientCall represents a detected HTTP client invocation.
type ClientCall struct {
	Path   string // URL path extracted from the call
	Method string // HTTP method or "" if unknown
	File   string // relative file path where the call is made
	Line   int    // line number
	NodeID int64  // DB node ID of the file (or nearest function)
}

// Route definition patterns (Python Flask/FastAPI, Go, Express/JS).
var routePatterns = []*regexp.Regexp{
	// Python: @app.route("/path") or @router.get("/path")
	regexp.MustCompile(`^\s*@(?:app|router)\.(get|post|put|delete|patch|route)\s*\(\s*["']([^"']+)["']`),
	// Go: r.HandleFunc("/path", ...) or mux.Handle("/path", ...)
	regexp.MustCompile(`^\s*[\w.]+\.(HandleFunc|Handle)\s*\(\s*["']([^"']+)["']`),
	// JS/TS: app.get("/path", ...) or router.post("/path", ...)
	regexp.MustCompile(`^\s*(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*["']([^"']+)["']`),
}

// Client call patterns (requests, httpx, fetch, axios, Go http).
var clientPatterns = []*regexp.Regexp{
	// Python: requests.get("..."), httpx.post("..."), aiohttp.get("...")
	regexp.MustCompile(`(?:requests|httpx|aiohttp)\.(get|post|put|delete|patch)\s*\(\s*["']([^"']+)["']`),
	// JS/TS: fetch("...")
	regexp.MustCompile(`fetch\s*\(\s*["']([^"']+)["']`),
	// JS/TS: axios.get("..."), http.get("...")
	regexp.MustCompile(`(?:axios|http)\.(get|post|put|delete|patch)\s*\(\s*["']([^"']+)["']`),
	// Go: http.Get("..."), http.Post("...")
	regexp.MustCompile(`http\.(Get|Post)\s*\(\s*["']([^"']+)["']`),
}

// normalizePath strips path parameters, trailing slashes, query strings, and
// host prefixes to produce a canonical path for matching.
// e.g. "/api/users/{id}" -> "/api/users", "http://svc/api/users?q=1" -> "/api/users"
func normalizePath(raw string) string {
	p := raw

	// Strip protocol + host prefix (http://host/path -> /path)
	if idx := strings.Index(p, "://"); idx != -1 {
		rest := p[idx+3:]
		if slashIdx := strings.Index(rest, "/"); slashIdx != -1 {
			p = rest[slashIdx:]
		} else {
			return "/"
		}
	}

	// Strip query string
	if idx := strings.Index(p, "?"); idx != -1 {
		p = p[:idx]
	}

	// Strip fragment
	if idx := strings.Index(p, "#"); idx != -1 {
		p = p[:idx]
	}

	// Drop ONLY DECLARED path-parameter segments: {id}, :id, <id>. (P2-10) Numeric /
	// uuid segments are KEPT in the match key: dropping them collapsed distinct
	// concrete routes (`/orders/42/items` and `/orders/99/items`) onto the same key,
	// minting a false API_CALL between unrelated endpoints. A concrete-literal path now
	// matches only another concrete-literal path with the same value — correct-or-quiet
	// (it trades route↔parameterized-route recall for precision, the intended tradeoff).
	segments := strings.Split(p, "/")
	var cleaned []string
	for _, seg := range segments {
		if seg == "" {
			continue
		}
		// Skip ONLY declared parameter segments ({id} / :id / <id>).
		if strings.HasPrefix(seg, "{") || strings.HasPrefix(seg, ":") || strings.HasPrefix(seg, "<") {
			continue
		}
		cleaned = append(cleaned, seg)
	}

	if len(cleaned) == 0 {
		return "/"
	}
	return "/" + strings.Join(cleaned, "/")
}

// (P2-10 removed) isLikelyValue — concrete numeric/uuid segments are now KEPT in the
// normalized match key (precision over recall), so the value-detection helper that
// dropped them is dead and removed (no dead control).

// apiRouteConfidence scales an API_CALL edge's confidence by how many routes a client
// path matched (route ambiguity): 1 unique route is the only verified case (0.7); 2 or
// more means the called endpoint is one of several same-path declarations, so the edge
// is a guess that must score below the strong tier. Mirrors the name_match ambiguity
// gradient (CLAUDE.md confidence model).
func apiRouteConfidence(matched int) float64 {
	switch {
	case matched <= 1:
		return 0.7
	case matched == 2:
		return 0.5
	case matched <= 5:
		return 0.4
	default:
		return 0.2
	}
}

// extractMethod normalizes an HTTP method string to uppercase.
func extractMethod(raw string) string {
	switch strings.ToLower(raw) {
	case "get":
		return "GET"
	case "post":
		return "POST"
	case "put":
		return "PUT"
	case "delete":
		return "DELETE"
	case "patch":
		return "PATCH"
	case "route", "handlefunc", "handle":
		return ""
	default:
		return strings.ToUpper(raw)
	}
}

// isAPIPath returns true if the path looks like an API route (starts with /api, /v1, etc.)
func isAPIPath(p string) bool {
	if !strings.HasPrefix(p, "/") {
		return false
	}
	if p == "/" {
		return false
	}
	return true
}

// ResolveAPIEdges scans source files for HTTP route definitions and client calls,
// then creates API_CALL edges between files that share matching paths.
func ResolveAPIEdges(db *store.DB, files []walker.SourceFile, root string) (int, error) {
	var routes []RouteDefinition
	var clients []ClientCall

	// Build a map of file_path -> node ID (use first node in that file as anchor).
	fileNodeMap := buildFileNodeMap(db, files)

	// Scan each file for route/client patterns.
	for _, sf := range files {
		absPath := sf.AbsPath
		if absPath == "" {
			absPath = root + "/" + sf.Path
		}

		f, err := os.Open(absPath)
		if err != nil {
			continue // skip unreadable files
		}

		scanner := bufio.NewScanner(f)
		lineNum := 0
		for scanner.Scan() {
			lineNum++
			line := scanner.Text()

			// Framework adapters run beside the legacy route patterns.  They are
			// producer-owned facts and carry language/framework/mechanism identity.
			for _, frameworkRoute := range ExtractFrameworkRoutes(line) {
				if frameworkRoute.Path == "/" && frameworkRoute.Mechanism != "app_router_handler" {
					continue
				}
				routes = append(routes, RouteDefinition{
					Path: frameworkRoute.Path, Method: frameworkRoute.Method,
					File: sf.Path, Line: lineNum, NodeID: fileNodeMap[sf.Path],
					Language: frameworkRoute.Language, Framework: frameworkRoute.Framework,
					Mechanism: frameworkRoute.Mechanism,
				})
			}

			// Check route patterns
			for _, re := range routePatterns {
				matches := re.FindStringSubmatch(line)
				if matches == nil {
					continue
				}
				var method, path string
				if len(matches) == 3 {
					method = extractMethod(matches[1])
					path = matches[2]
				} else if len(matches) == 2 {
					path = matches[1]
				}
				norm := normalizePath(path)
				if !isAPIPath(norm) {
					continue
				}
				routes = append(routes, RouteDefinition{
					Path:   norm,
					Method: method,
					File:   sf.Path,
					Line:   lineNum,
					NodeID: fileNodeMap[sf.Path],
				})
			}

			// Check client patterns
			for _, re := range clientPatterns {
				matches := re.FindStringSubmatch(line)
				if matches == nil {
					continue
				}
				var method, path string
				if len(matches) == 3 {
					method = extractMethod(matches[1])
					path = matches[2]
				} else if len(matches) == 2 {
					path = matches[1]
					method = ""
				}
				norm := normalizePath(path)
				if !isAPIPath(norm) {
					continue
				}
				clients = append(clients, ClientCall{
					Path:   norm,
					Method: method,
					File:   sf.Path,
					Line:   lineNum,
					NodeID: fileNodeMap[sf.Path],
				})
			}
		}
		f.Close()
	}

	// Match routes to clients by normalized path.
	// Build index: normalized path -> []RouteDefinition
	// Adapters and legacy patterns can recognize the same source line; collapse
	// those duplicates while preferring the framework-labelled fact.
	dedupedRoutes := make([]RouteDefinition, 0, len(routes))
	routeSeen := make(map[string]int)
	for _, route := range routes {
		key := fmt.Sprintf("%s|%s|%d|%s", route.File, route.Path, route.Line, route.Method)
		if index, ok := routeSeen[key]; ok {
			if dedupedRoutes[index].Framework == "" && route.Framework != "" {
				dedupedRoutes[index] = route
			}
			continue
		}
		routeSeen[key] = len(dedupedRoutes)
		dedupedRoutes = append(dedupedRoutes, route)
	}
	routes = dedupedRoutes
	routeIndex := make(map[string][]RouteDefinition)
	for _, r := range routes {
		routeIndex[r.Path] = append(routeIndex[r.Path], r)
	}

	// For each client call, find matching routes and create edges.
	var edges []*store.Edge
	seen := make(map[edgeKey]bool)

	for _, c := range clients {
		matchedRoutes := routeIndex[c.Path]
		for _, r := range matchedRoutes {
			// Don't create self-edges (same file).
			if c.File == r.File {
				continue
			}
			// Skip if either node is missing from DB.
			if c.NodeID == 0 || r.NodeID == 0 {
				continue
			}

			key := edgeKey{sourceID: c.NodeID, targetID: r.NodeID, typ: "API_CALL"}
			if seen[key] {
				continue
			}
			seen[key] = true

			metadata, _ := json.Marshal(map[string]string{
				"route": c.Path, "method": c.Method,
				"framework": r.Framework, "language": r.Language,
				"mechanism": r.Mechanism,
			})

			// #2: API_CALL edges previously left trust_tier/evidence_type/
			// verification_status as empty strings (the explicit empty bind in
			// BatchInsertEdges defeats the SQL column DEFAULTs). Stamp the tier
			// from confidence via the ONE threshold table (tierFor) and record
			// route ambiguity honestly in candidate_count.
			// P2-10: SCALE confidence by route ambiguity. A client path matching N
			// distinct routes resolves to an arbitrary one of them — its confidence must
			// fall with N, not stay flat at 0.7. (1→0.7, 2→0.5, 3-5→0.4, >5→0.2.)
			conf := apiRouteConfidence(len(matchedRoutes))
			edges = append(edges, &store.Edge{
				SourceID:           c.NodeID,
				TargetID:           r.NodeID,
				Type:               "API_CALL",
				SourceLine:         c.Line,
				SourceFile:         c.File,
				ResolutionMethod:   "route_match",
				Confidence:         conf,
				TrustTier:          tierFor(conf),
				CandidateCount:     len(matchedRoutes),
				EvidenceType:       "route_match",
				VerificationStatus: "unverified",
				Metadata:           string(metadata),
			})
		}
	}

	if len(edges) == 0 {
		return 0, nil
	}

	if err := db.BatchInsertEdges(edges); err != nil {
		return 0, fmt.Errorf("insert API edges: %w", err)
	}

	return len(edges), nil
}

// buildFileNodeMap queries the DB for the first node in each file to use as an
// anchor for API edges. Falls back to 0 if no node exists for that file.
func buildFileNodeMap(db *store.DB, files []walker.SourceFile) map[string]int64 {
	result := make(map[string]int64, len(files))
	// Use LookupNodeByName won't work here — we need file-level lookup.
	// Query directly via a transaction.
	tx, err := db.BeginTx()
	if err != nil {
		return result
	}
	defer tx.Rollback()

	stmt, err := tx.Prepare(`SELECT id FROM nodes WHERE file_path = ? ORDER BY id LIMIT 1`)
	if err != nil {
		return result
	}
	defer stmt.Close()

	for _, sf := range files {
		var id int64
		if err := stmt.QueryRow(sf.Path).Scan(&id); err == nil {
			result[sf.Path] = id
		}
	}
	return result
}
