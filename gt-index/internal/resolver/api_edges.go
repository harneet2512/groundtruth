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
	Path   string // normalized URL path, e.g. "/api/users"
	Method string // HTTP method (GET, POST, etc.) or "" if unknown
	File   string // relative file path where the route is defined
	Line   int    // line number
	NodeID int64  // DB node ID of the file (or nearest function)
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
	regexp.MustCompile(`@(?:app|router)\.(get|post|put|delete|patch|route)\s*\(\s*["']([^"']+)["']`),
	// Go: r.HandleFunc("/path", ...) or mux.Handle("/path", ...)
	regexp.MustCompile(`\.(HandleFunc|Handle)\s*\(\s*["']([^"']+)["']`),
	// JS/TS: app.get("/path", ...) or router.post("/path", ...)
	regexp.MustCompile(`(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*["']([^"']+)["']`),
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

	// Remove path parameter segments: {id}, :id, <id>
	segments := strings.Split(p, "/")
	var cleaned []string
	for _, seg := range segments {
		if seg == "" {
			continue
		}
		// Skip parameter segments
		if strings.HasPrefix(seg, "{") || strings.HasPrefix(seg, ":") || strings.HasPrefix(seg, "<") {
			continue
		}
		// Skip segments that look like interpolated values (purely numeric, uuid-like)
		if isLikelyValue(seg) {
			continue
		}
		cleaned = append(cleaned, seg)
	}

	if len(cleaned) == 0 {
		return "/"
	}
	return "/" + strings.Join(cleaned, "/")
}

// isLikelyValue returns true if a path segment looks like a runtime value (number, uuid).
func isLikelyValue(seg string) bool {
	// Pure digits
	allDigits := true
	for _, c := range seg {
		if c < '0' || c > '9' {
			allDigits = false
			break
		}
	}
	if allDigits && len(seg) > 0 {
		return true
	}
	// UUID-shaped (8-4-4-4-12 hex)
	if len(seg) == 36 && seg[8] == '-' && seg[13] == '-' {
		return true
	}
	return false
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
				"route":  c.Path,
				"method": c.Method,
			})

			edges = append(edges, &store.Edge{
				SourceID:         c.NodeID,
				TargetID:         r.NodeID,
				Type:             "API_CALL",
				SourceLine:       c.Line,
				SourceFile:       c.File,
				ResolutionMethod: "route_match",
				Confidence:       0.7,
				Metadata:         string(metadata),
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
