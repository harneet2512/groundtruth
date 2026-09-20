package resolver

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ----------------------------------------------------------------------------
// P2b: HANDLES_ROUTE for the non-Python manifest languages. Each fixture is a
// small source file with a route registration + handler node; the assertions
// pin the edge landing handler -> file anchor, the confidence tier, and the
// {route, method, framework, mechanism} metadata consumers read.
// ----------------------------------------------------------------------------

// routeFixture writes src files into a temp repo and opens graph.db in it.
func routeFixture(t *testing.T, src map[string]string, langs map[string]string) (*store.DB, []walker.SourceFile, string) {
	t.Helper()
	root := t.TempDir()
	var files []walker.SourceFile
	// Deterministic file order for the scan.
	names := make([]string, 0, len(src))
	for name := range src {
		names = append(names, name)
	}
	for i := 0; i < len(names); i++ {
		for j := i + 1; j < len(names); j++ {
			if names[j] < names[i] {
				names[i], names[j] = names[j], names[i]
			}
		}
	}
	for _, name := range names {
		p := filepath.Join(root, name)
		if dir := filepath.Dir(p); dir != root {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.WriteFile(p, []byte(src[name]), 0o644); err != nil {
			t.Fatal(err)
		}
		files = append(files, walker.SourceFile{Path: name, AbsPath: p, Language: langs[name]})
	}
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db, files, root
}

// queryHandlesRoute returns all HANDLES_ROUTE edges keyed by (source,target).
func queryHandlesRoute(t *testing.T, db *store.DB) map[[2]int64]store.Edge {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	rows, err := tx.Query(`SELECT source_id, target_id, COALESCE(resolution_method,''),
	        COALESCE(confidence,0), COALESCE(metadata,''), COALESCE(source_line,0),
	        COALESCE(trust_tier,'')
	   FROM edges WHERE type = 'HANDLES_ROUTE'`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := make(map[[2]int64]store.Edge)
	for rows.Next() {
		var e store.Edge
		if err := rows.Scan(&e.SourceID, &e.TargetID, &e.ResolutionMethod,
			&e.Confidence, &e.Metadata, &e.SourceLine, &e.TrustTier); err != nil {
			t.Fatal(err)
		}
		out[[2]int64{e.SourceID, e.TargetID}] = e
	}
	return out
}

// routeMeta decodes the edge metadata JSON into a map for field assertions.
func routeMeta(t *testing.T, e store.Edge) map[string]string {
	t.Helper()
	if e.Metadata == "" {
		return nil
	}
	var m map[string]string
	if err := json.Unmarshal([]byte(e.Metadata), &m); err != nil {
		t.Fatalf("edge metadata is not JSON: %q", e.Metadata)
	}
	return m
}

// TestHandlesRouteJSExpress covers Express registration binding: the handler is
// the LAST top-level argument after the path literal. Middleware names, inline
// handlers, and unresolvable references must abstain.
func TestHandlesRouteJSExpress(t *testing.T) {
	src := `const express = require('express');
const app = express();

function getUsers(req, res) {}
function createOrder(req, res) {}
function auth(req, res, next) {}

app.get("/api/users", getUsers);
app.post("/api/orders", auth, createOrder);
app.put("/api/inline", (req, res) => res.send("x"));
app.delete("/api/missing", missingHandler);
`
	db, files, root := routeFixture(t,
		map[string]string{"app.js": src},
		map[string]string{"app.js": "javascript"})

	// id 1 is the file anchor (first node); handlers are ids 2-4.
	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, language) VALUES
		(1, 'Function', 'initApp',     'app.js', 1, 'javascript'),
		(2, 'Function', 'getUsers',    'app.js', 4, 'javascript'),
		(3, 'Function', 'createOrder', 'app.js', 5, 'javascript'),
		(4, 'Function', 'auth',        'app.js', 6, 'javascript')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryHandlesRoute(t, db)

	// app.get("/api/users", getUsers) -> getUsers (2) handles the route.
	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE getUsers -> app.js anchor")
	}
	if e.ResolutionMethod != "framework_route" {
		t.Errorf("resolution_method = %q, want framework_route", e.ResolutionMethod)
	}
	if e.Confidence != 0.7 {
		t.Errorf("confidence = %.2f, want 0.7 (registration is a looser match)", e.Confidence)
	}
	if e.SourceLine != 8 {
		t.Errorf("source_line = %d, want 8 (the registration line)", e.SourceLine)
	}
	m := routeMeta(t, e)
	if m["route"] != "/api/users" || m["method"] != "GET" || m["framework"] != "Express" || m["mechanism"] != "route_registration" {
		t.Errorf("metadata = %v, want route=/api/users method=GET framework=Express mechanism=route_registration", m)
	}

	// app.post("/api/orders", auth, createOrder) -> createOrder (3), NOT auth (4):
	// the LAST top-level argument is the handler.
	e2, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE createOrder -> app.js anchor")
	}
	if m2 := routeMeta(t, e2); m2["route"] != "/api/orders" || m2["method"] != "POST" {
		t.Errorf("metadata = %v, want route=/api/orders method=POST", m2)
	}
	if _, ok := edges[[2]int64{4, 1}]; ok {
		t.Error("auth (middleware arg) bound as handler — last-arg rule broken")
	}

	// Abstentions: inline arrow handler and unresolvable name mint no edge.
	if len(edges) != 2 {
		t.Errorf("got %d HANDLES_ROUTE edges, want exactly 2 (inline + missing handler must abstain)", len(edges))
	}
}

// TestHandlesRouteTSNestDecorator covers NestJS decorator binding: @Get/@Post on
// a controller method binds the NEXT method declaration (adjacent binding,
// 0.95 / decorator_route). A stacked non-route decorator must not break the
// binding.
func TestHandlesRouteTSNestDecorator(t *testing.T) {
	src := `import { Controller, Get, Post, HttpCode } from '@nestjs/common';

@Controller('/api')
export class UserController {
  @Get('/api/users')
  getUsers() { return []; }

  @Post('/api/orders')
  @HttpCode(201)
  async createOrder() { return null; }
}
`
	db, files, root := routeFixture(t,
		map[string]string{"controller.ts": src},
		map[string]string{"controller.ts": "typescript"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, language) VALUES
		(1, 'Class',  'UserController', 'controller.ts', 4, 'typescript'),
		(2, 'Method', 'getUsers',       'controller.ts', 6, 'typescript'),
		(3, 'Method', 'createOrder',    'controller.ts', 10, 'typescript')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryHandlesRoute(t, db)

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE getUsers -> controller.ts anchor")
	}
	if e.ResolutionMethod != "decorator_route" {
		t.Errorf("resolution_method = %q, want decorator_route", e.ResolutionMethod)
	}
	if e.Confidence != 0.95 {
		t.Errorf("confidence = %.2f, want 0.95 (adjacent decorator binding)", e.Confidence)
	}
	if e.SourceLine != 5 {
		t.Errorf("source_line = %d, want 5 (the @Get decorator line)", e.SourceLine)
	}
	m := routeMeta(t, e)
	if m["route"] != "/api/users" || m["method"] != "GET" || m["framework"] != "NestJS" {
		t.Errorf("metadata = %v, want route=/api/users method=GET framework=NestJS", m)
	}

	// @Post + stacked @HttpCode: the route still binds createOrder.
	e2, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE createOrder -> controller.ts anchor (stacked decorator broke the pending route)")
	}
	if m2 := routeMeta(t, e2); m2["route"] != "/api/orders" || m2["method"] != "POST" {
		t.Errorf("metadata = %v, want route=/api/orders method=POST", m2)
	}
	if e2.SourceLine != 8 {
		t.Errorf("source_line = %d, want 8 (the @Post decorator line)", e2.SourceLine)
	}

	if len(edges) != 2 {
		t.Errorf("got %d HANDLES_ROUTE edges, want exactly 2 (@Controller must not mint)", len(edges))
	}
}

// TestHandlesRouteNextAppRouter covers the Next.js file convention: an
// `export function GET` in an app-router route file binds itself at 0.8, with
// the route path derived from the directory convention. The same export in a
// non-route file must abstain.
func TestHandlesRouteNextAppRouter(t *testing.T) {
	routeSrc := `import { NextResponse } from 'next/server';

export async function GET(request: Request) {
  return NextResponse.json([]);
}

export function POST() {
  return NextResponse.json({});
}
`
	utilSrc := `export function GET() {
  return "not a route handler";
}
`
	db, files, root := routeFixture(t,
		map[string]string{
			"app/api/users/route.ts": routeSrc,
			"lib/utils.ts":           utilSrc,
		},
		map[string]string{
			"app/api/users/route.ts": "typescript",
			"lib/utils.ts":           "typescript",
		})

	// route.ts: id 1 anchors the file (a preceding non-handler node), GET/POST
	// are the exported handlers. utils.ts: id 4 anchors, its GET is id 5 so a
	// wrongful emission would be detectable as 5->4.
	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, language) VALUES
		(1, 'Function', 'config', 'app/api/users/route.ts', 1, 'typescript'),
		(2, 'Function', 'GET',    'app/api/users/route.ts', 3, 'typescript'),
		(3, 'Function', 'POST',   'app/api/users/route.ts', 7, 'typescript'),
		(4, 'Function', 'alpha',  'lib/utils.ts',           1, 'typescript'),
		(5, 'Function', 'GET',    'lib/utils.ts',           2, 'typescript')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryHandlesRoute(t, db)

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE GET -> route.ts anchor")
	}
	if e.ResolutionMethod != "framework_route" || e.Confidence != 0.8 {
		t.Errorf("method=%q conf=%.2f, want framework_route/0.8 (file-convention binding)", e.ResolutionMethod, e.Confidence)
	}
	m := routeMeta(t, e)
	if m["route"] != "/api/users" || m["method"] != "GET" || m["framework"] != "Next.js" || m["mechanism"] != "app_router_handler" {
		t.Errorf("metadata = %v, want route=/api/users method=GET framework=Next.js mechanism=app_router_handler", m)
	}

	e2, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE POST -> route.ts anchor")
	}
	if m2 := routeMeta(t, e2); m2["route"] != "/api/users" || m2["method"] != "POST" {
		t.Errorf("metadata = %v, want route=/api/users method=POST", m2)
	}

	// lib/utils.ts is not an app-router route file: its `export function GET`
	// must not mint an edge.
	if _, ok := edges[[2]int64{5, 4}]; ok {
		t.Error("export function GET in a non-route file minted a HANDLES_ROUTE — file-convention gate missing")
	}
	if len(edges) != 2 {
		t.Errorf("got %d HANDLES_ROUTE edges, want exactly 2", len(edges))
	}
}

// TestHandlesRouteJavaSpring covers Spring annotation binding: @GetMapping and
// @RequestMapping(value = ...) bind the next method declaration at 0.95 /
// decorator_route. The `value =` attribute form must also be caught.
func TestHandlesRouteJavaSpring(t *testing.T) {
	src := `@RestController
public class UserController {

    @GetMapping("/api/users")
    public List<User> getUsers() { return null; }

    @RequestMapping(value = "/api/legacy")
    public String legacy() { return ""; }
}
`
	db, files, root := routeFixture(t,
		map[string]string{"UserController.java": src},
		map[string]string{"UserController.java": "java"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, language) VALUES
		(1, 'Class',  'UserController', 'UserController.java', 2, 'java'),
		(2, 'Method', 'getUsers',       'UserController.java', 5, 'java'),
		(3, 'Method', 'legacy',         'UserController.java', 8, 'java')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryHandlesRoute(t, db)

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE getUsers -> UserController.java anchor")
	}
	if e.ResolutionMethod != "decorator_route" || e.Confidence != 0.95 {
		t.Errorf("method=%q conf=%.2f, want decorator_route/0.95", e.ResolutionMethod, e.Confidence)
	}
	if e.SourceLine != 4 {
		t.Errorf("source_line = %d, want 4 (the @GetMapping line)", e.SourceLine)
	}
	m := routeMeta(t, e)
	if m["route"] != "/api/users" || m["method"] != "GET" || m["framework"] != "Spring" || m["mechanism"] != "request_mapping" {
		t.Errorf("metadata = %v, want route=/api/users method=GET framework=Spring mechanism=request_mapping", m)
	}

	// @RequestMapping(value = "/api/legacy") — the value= form the literal-only
	// framework pattern misses; javaRouteMappingRe must catch it. Method stays
	// "" (RequestMapping carries no fixed verb).
	e2, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE legacy -> anchor (value= attribute form not caught)")
	}
	m2 := routeMeta(t, e2)
	if m2["route"] != "/api/legacy" || m2["method"] != "" || m2["framework"] != "Spring" {
		t.Errorf("metadata = %v, want route=/api/legacy method='' framework=Spring", m2)
	}

	if len(edges) != 2 {
		t.Errorf("got %d HANDLES_ROUTE edges, want exactly 2 (class-level @RestController must not mint)", len(edges))
	}
}

// TestHandlesRouteGo covers Go registration binding: gin/echo `r.GET(path, h)`
// and net/http `mux.HandleFunc(path, h)`. The handler is the argument after the
// path literal; an inline `func(...)` must abstain.
func TestHandlesRouteGo(t *testing.T) {
	src := `package main

import "net/http"

func main() {
	r := setupRouter()
	r.GET("/api/users", listUsers)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", healthCheck)
	http.HandleFunc("/api/internal", func(w http.ResponseWriter, r *http.Request) {})
}

func listUsers() {}
func healthCheck() {}
`
	db, files, root := routeFixture(t,
		map[string]string{"server.go": src},
		map[string]string{"server.go": "go"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, language) VALUES
		(1, 'Function', 'main',        'server.go', 5,  'go'),
		(2, 'Function', 'listUsers',   'server.go', 13, 'go'),
		(3, 'Function', 'healthCheck', 'server.go', 14, 'go')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryHandlesRoute(t, db)

	// gin/echo-style r.GET -> listUsers.
	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE listUsers -> server.go anchor")
	}
	if e.ResolutionMethod != "framework_route" || e.Confidence != 0.7 {
		t.Errorf("method=%q conf=%.2f, want framework_route/0.7", e.ResolutionMethod, e.Confidence)
	}
	m := routeMeta(t, e)
	if m["route"] != "/api/users" || m["method"] != "GET" || m["framework"] != "gin/echo" || m["mechanism"] != "route_registration" {
		t.Errorf("metadata = %v, want route=/api/users method=GET framework=gin/echo mechanism=route_registration", m)
	}

	// net/http-style mux.HandleFunc -> healthCheck (method unknown).
	e2, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE healthCheck -> server.go anchor")
	}
	m2 := routeMeta(t, e2)
	if m2["route"] != "/api/health" || m2["method"] != "" || m2["framework"] != "net/http" {
		t.Errorf("metadata = %v, want route=/api/health method='' framework=net/http", m2)
	}

	// The inline func(...) handler for /api/internal mints no edge.
	if len(edges) != 2 {
		t.Errorf("got %d HANDLES_ROUTE edges, want exactly 2 (inline func handler must abstain)", len(edges))
	}
}

// TestHandlesRoutePythonUnchanged pins the pre-existing Python decorator path:
// handler -> file anchor, decorator_route, 0.95, and NO route metadata (the
// consumer re-reads source_line — the new non-Python edges must not change it).
func TestHandlesRoutePythonUnchanged(t *testing.T) {
	src := `def helper():
    pass

@app.route("/api/ping")
def ping():
    return "pong"
`
	db, files, root := routeFixture(t,
		map[string]string{"routes.py": src},
		map[string]string{"routes.py": "python"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, language) VALUES
		(1, 'Function', 'helper', 'routes.py', 1, 'python'),
		(2, 'Function', 'ping',   'routes.py', 5, 'python')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryHandlesRoute(t, db)

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing HANDLES_ROUTE ping -> routes.py anchor")
	}
	if e.ResolutionMethod != "decorator_route" || e.Confidence != 0.95 {
		t.Errorf("method=%q conf=%.2f, want decorator_route/0.95", e.ResolutionMethod, e.Confidence)
	}
	if e.SourceLine != 4 {
		t.Errorf("source_line = %d, want 4 (the @app.route line)", e.SourceLine)
	}
	if e.Metadata != "" {
		t.Errorf("python HANDLES_ROUTE metadata = %q, want empty (consumer re-reads source_line)", e.Metadata)
	}
	if len(edges) != 1 {
		t.Errorf("got %d HANDLES_ROUTE edges, want exactly 1", len(edges))
	}
}
