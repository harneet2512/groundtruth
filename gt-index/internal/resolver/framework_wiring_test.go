package resolver

import (
	"encoding/json"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// ----------------------------------------------------------------------------
// HAR-90 item 3: framework wiring — MIDDLEWARE_ON (Express/Django/NestJS/
// Spring) and INJECTS (Spring @Autowired, NestJS ctor, FastAPI Depends).
// Each fixture is a small source file with pre-seeded nodes; assertions pin
// edge endpoints, the mechanism metadata, the confidence contract
// (framework-declared 0.9 / resolved-impl 0.6 / ambiguous candidate 0.4), and
// the abstention rules (unresolvable or ambiguous names mint nothing).
// ----------------------------------------------------------------------------

// queryWiringEdges returns all edges of `typ` keyed by (source,target),
// including candidate_count for the ambiguity assertions.
func queryWiringEdges(t *testing.T, db *store.DB, typ string) map[[2]int64]store.Edge {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	rows, err := tx.Query(`SELECT source_id, target_id, COALESCE(resolution_method,''),
	        COALESCE(confidence,0), COALESCE(metadata,''), COALESCE(source_line,0),
	        COALESCE(trust_tier,''), COALESCE(candidate_count,0)
	   FROM edges WHERE type = ?`, typ)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := make(map[[2]int64]store.Edge)
	for rows.Next() {
		var e store.Edge
		if err := rows.Scan(&e.SourceID, &e.TargetID, &e.ResolutionMethod,
			&e.Confidence, &e.Metadata, &e.SourceLine, &e.TrustTier,
			&e.CandidateCount); err != nil {
			t.Fatal(err)
		}
		out[[2]int64{e.SourceID, e.TargetID}] = e
	}
	return out
}

// wiringMeta decodes edge metadata JSON tolerantly — `route` may be null.
func wiringMeta(t *testing.T, e store.Edge) map[string]any {
	t.Helper()
	if e.Metadata == "" {
		return nil
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(e.Metadata), &m); err != nil {
		t.Fatalf("edge metadata is not JSON: %q", e.Metadata)
	}
	return m
}

// TestMiddlewareExpress covers app.use(fn) / app.use(path, fn) / router.use(fn):
// module-level middleware targets the file anchor; a leading path literal lands
// in metadata.route; an unresolvable middleware name abstains.
func TestMiddlewareExpress(t *testing.T) {
	src := `const express = require('express');
const app = express();
const router = express.Router();

function bootstrap() {}
function auth(req, res, next) { next(); }
function logReq(req, res, next) { next(); }
function track(req, res, next) { next(); }

app.use(auth);
app.use('/api', logReq);
router.use(track);
app.use(missingMw);
`
	db, files, root := routeFixture(t,
		map[string]string{"app.js": src},
		map[string]string{"app.js": "javascript"})

	// id 1 anchors the file (first node in app.js).
	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Function', 'bootstrap', 'app.js', 5, 5, 'javascript'),
		(2, 'Function', 'auth',      'app.js', 6, 6, 'javascript'),
		(3, 'Function', 'logReq',    'app.js', 7, 7, 'javascript'),
		(4, 'Function', 'track',     'app.js', 8, 8, 'javascript')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "MIDDLEWARE_ON")

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing MIDDLEWARE_ON auth -> app.js anchor")
	}
	if e.ResolutionMethod != "express_use" || e.Confidence != 0.9 {
		t.Errorf("method=%q conf=%.2f, want express_use/0.9", e.ResolutionMethod, e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["mechanism"] != "express_use" || m["route"] != nil {
		t.Errorf("metadata = %v, want mechanism=express_use route=null", m)
	}

	e2, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing MIDDLEWARE_ON logReq -> app.js anchor")
	}
	if m2 := wiringMeta(t, e2); m2["route"] != "/api" {
		t.Errorf("route-scoped use() metadata = %v, want route=/api", m2)
	}

	if _, ok := edges[[2]int64{4, 1}]; !ok {
		t.Error("missing MIDDLEWARE_ON track -> anchor (router.use must bind like app.use)")
	}

	// missingMw resolves to nothing — no edge; exactly 3 edges total.
	if len(edges) != 3 {
		t.Errorf("got %d MIDDLEWARE_ON edges, want exactly 3 (unresolvable mw abstains)", len(edges))
	}
}

// TestMiddlewareDjango covers the settings MIDDLEWARE list: dotted entries
// resolve through the `a.b.C` -> `a/b.py` file hint to the middleware class,
// targeted at the settings file's anchor node.
func TestMiddlewareDjango(t *testing.T) {
	settings := `import os

MIDDLEWARE = [
    'myapp.middleware.AuthMiddleware',
    'myapp.middleware.SessionMiddleware',
    # 'myapp.middleware.CommentedOut',
    'other.middleware.Missing',
]
`
	mw := `class AuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

class SessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
`
	db, files, root := routeFixture(t,
		map[string]string{
			"myapp/settings.py":   settings,
			"myapp/middleware.py": mw,
		},
		map[string]string{
			"myapp/settings.py":   "python",
			"myapp/middleware.py": "python",
		})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'File',  'settings',          'myapp/settings.py',   1, 9, 'python'),
		(2, 'Class', 'AuthMiddleware',    'myapp/middleware.py', 1, 3, 'python'),
		(3, 'Class', 'SessionMiddleware', 'myapp/middleware.py', 5, 7, 'python')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "MIDDLEWARE_ON")

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing MIDDLEWARE_ON AuthMiddleware -> settings anchor")
	}
	if e.ResolutionMethod != "django_middleware" || e.Confidence != 0.9 {
		t.Errorf("method=%q conf=%.2f, want django_middleware/0.9", e.ResolutionMethod, e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["mechanism"] != "django_middleware" || m["route"] != nil {
		t.Errorf("metadata = %v, want mechanism=django_middleware route=null", m)
	}

	if _, ok := edges[[2]int64{3, 1}]; !ok {
		t.Error("missing MIDDLEWARE_ON SessionMiddleware -> settings anchor")
	}
	// The commented-out entry and the unresolvable Missing mint nothing.
	if len(edges) != 2 {
		t.Errorf("got %d MIDDLEWARE_ON edges, want exactly 2 (comment + missing abstain)", len(edges))
	}
}

// TestMiddlewareNest covers consumer.apply(M).forRoutes(...) inside a module
// configure(): the middleware class is wired to the module class (the wiring
// statement's owner), with the forRoutes scope in metadata.route. Both the
// same-line chain and the continuation-line chain are covered.
func TestMiddlewareNest(t *testing.T) {
	src := `import { Module, NestModule, MiddlewareConsumer } from '@nestjs/common';
import { LoggerMiddleware } from './logger';
import { CorsMiddleware } from './cors';

@Module({})
export class AppModule implements NestModule {
  configure(consumer: MiddlewareConsumer) {
    consumer.apply(LoggerMiddleware).forRoutes('cats');
    consumer
      .apply(CorsMiddleware)
      .forRoutes({ path: 'api/*', method: RequestMethod.ALL });
  }
}
`
	db, files, root := routeFixture(t,
		map[string]string{"app.module.ts": src},
		map[string]string{"app.module.ts": "typescript"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Class',  'AppModule',        'app.module.ts', 5, 13, 'typescript'),
		(2, 'Method', 'configure',        'app.module.ts', 7, 12, 'typescript'),
		(3, 'Class',  'LoggerMiddleware', 'app.module.ts', 2, 2,  'typescript'),
		(4, 'Class',  'CorsMiddleware',   'app.module.ts', 3, 3,  'typescript')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "MIDDLEWARE_ON")

	e, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing MIDDLEWARE_ON LoggerMiddleware -> AppModule")
	}
	if e.ResolutionMethod != "nest_consumer" || e.Confidence != 0.9 {
		t.Errorf("method=%q conf=%.2f, want nest_consumer/0.9", e.ResolutionMethod, e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["mechanism"] != "nest_consumer" || m["route"] != "cats" {
		t.Errorf("metadata = %v, want mechanism=nest_consumer route=cats", m)
	}

	e2, ok := edges[[2]int64{4, 1}]
	if !ok {
		t.Fatal("missing MIDDLEWARE_ON CorsMiddleware -> AppModule (continuation-line chain)")
	}
	if m2 := wiringMeta(t, e2); m2["route"] != "api/*" {
		t.Errorf("metadata = %v, want route=api/* (object-form forRoutes)", m2)
	}
}

// TestMiddlewareSpring covers WebMvcConfigurer.addInterceptors: the
// `registry.addInterceptor(new X()).addPathPatterns(...)` chain wires the
// interceptor class onto the configurer class with the pattern as route.
func TestMiddlewareSpring(t *testing.T) {
	src := `public class WebConfig implements WebMvcConfigurer {
    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(new AuthInterceptor())
            .addPathPatterns("/api/**");
        registry.addInterceptor(new AuditInterceptor());
    }
}
`
	db, files, root := routeFixture(t,
		map[string]string{"WebConfig.java": src},
		map[string]string{"WebConfig.java": "java"})

	// The interceptor classes live OUTSIDE WebConfig's span — a nested-range
	// fixture would make enclosingClass pick them as the wiring owner (the
	// innermost-range rule) and the self-edge would drop.
	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Class',      'WebConfig',        'WebConfig.java', 1, 8, 'java'),
		(2, 'Method',     'addInterceptors',  'WebConfig.java', 3, 7, 'java'),
		(3, 'Class',      'AuthInterceptor',  'WebConfig.java', 10, 12, 'java'),
		(4, 'Class',      'AuditInterceptor', 'WebConfig.java', 14, 16, 'java')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "MIDDLEWARE_ON")

	e, ok := edges[[2]int64{3, 1}]
	if !ok {
		t.Fatal("missing MIDDLEWARE_ON AuthInterceptor -> WebConfig")
	}
	if e.ResolutionMethod != "spring_interceptor" || e.Confidence != 0.9 {
		t.Errorf("method=%q conf=%.2f, want spring_interceptor/0.9", e.ResolutionMethod, e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["mechanism"] != "spring_interceptor" || m["route"] != "/api/**" {
		t.Errorf("metadata = %v, want mechanism=spring_interceptor route=/api/**", m)
	}

	e2, ok := edges[[2]int64{4, 1}]
	if !ok {
		t.Fatal("missing MIDDLEWARE_ON AuditInterceptor -> WebConfig (chain-less registration)")
	}
	if m2 := wiringMeta(t, e2); m2["route"] != nil {
		t.Errorf("metadata = %v, want route=null (no addPathPatterns)", m2)
	}
}

// TestInjectsSpringAutowiredField covers field injection: `@Autowired private
// ItemService itemService;` binds the enclosing class to the declared type at
// 0.9 with spring_autowired provenance metadata.
func TestInjectsSpringAutowiredField(t *testing.T) {
	src := `public class ItemsController {
    @Autowired
    private ItemService itemService;

    @Autowired
    private int notAService;
}
`
	db, files, root := routeFixture(t,
		map[string]string{"ItemsController.java": src},
		map[string]string{"ItemsController.java": "java"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Class', 'ItemsController', 'ItemsController.java', 1, 7, 'java'),
		(2, 'Class', 'ItemService',     'ItemsController.java', 9, 9, 'java')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "INJECTS")

	e, ok := edges[[2]int64{1, 2}]
	if !ok {
		t.Fatal("missing INJECTS ItemsController -> ItemService")
	}
	if e.ResolutionMethod != "spring_autowired" || e.Confidence != 0.9 {
		t.Errorf("method=%q conf=%.2f, want spring_autowired/0.9", e.ResolutionMethod, e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["mechanism"] != "spring_autowired" || m["declared_type"] != "ItemService" || m["resolved_to"] != "ItemService" {
		t.Errorf("metadata = %v, want mechanism=spring_autowired declared_type=ItemService resolved_to=ItemService", m)
	}
	// The `int` field is a primitive — javaFieldDeclRe requires a capitalized
	// type, so it mints no fact and no edge.
	if len(edges) != 1 {
		t.Errorf("got %d INJECTS edges, want exactly 1 (primitive field abstains)", len(edges))
	}
}

// TestInjectsSpringInterfaceImpl covers the IMPLEMENTS hop: an @Autowired
// interface with exactly one implementation resolves to the impl at 0.6 —
// the name-match impl cap.
func TestInjectsSpringInterfaceImpl(t *testing.T) {
	src := `interface Repo { void find(); }

class RepoImpl implements Repo {
    public void find() {}
}

class ItemsController {
    @Autowired
    private Repo repo;
}
`
	db, files, root := routeFixture(t,
		map[string]string{"App.java": src},
		map[string]string{"App.java": "java"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Interface', 'Repo',            'App.java', 1, 1,  'java'),
		(2, 'Class',     'RepoImpl',        'App.java', 3, 5,  'java'),
		(3, 'Class',     'ItemsController', 'App.java', 7, 11, 'java'),
		(4, 'Method',    'find',            'App.java', 4, 4,  'java')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "INJECTS")

	// The in-flight IMPLEMENTS (RepoImpl -> Repo) lets the declared interface
	// hop to its single implementation.
	e, ok := edges[[2]int64{3, 2}]
	if !ok {
		t.Fatal("missing INJECTS ItemsController -> RepoImpl (IMPLEMENTS hop)")
	}
	if e.Confidence != 0.6 {
		t.Errorf("conf=%.2f, want 0.6 (name-match impl cap)", e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["declared_type"] != "Repo" || m["resolved_to"] != "RepoImpl" || m["ambiguous"] != false {
		t.Errorf("metadata = %v, want declared_type=Repo resolved_to=RepoImpl ambiguous=false", m)
	}
}

// TestInjectsSpringAmbiguousImpls covers the ambiguity contract: two
// implementations of the injected interface emit one 0.4 candidate edge each
// with ambiguous=true and candidate_count=2 — never a silent pick.
func TestInjectsSpringAmbiguousImpls(t *testing.T) {
	src := `interface Repo { void find(); }

class RepoImplA implements Repo {
    public void find() {}
}

class RepoImplB implements Repo {
    public void find() {}
}

class ItemsController {
    @Autowired
    private Repo repo;
}
`
	db, files, root := routeFixture(t,
		map[string]string{"App.java": src},
		map[string]string{"App.java": "java"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Interface', 'Repo',            'App.java', 1,  1,  'java'),
		(2, 'Class',     'RepoImplA',       'App.java', 3,  5,  'java'),
		(3, 'Class',     'RepoImplB',       'App.java', 7,  9,  'java'),
		(4, 'Class',     'ItemsController', 'App.java', 11, 15, 'java'),
		(5, 'Method',    'findA',           'App.java', 4,  4,  'java'),
		(6, 'Method',    'findB',           'App.java', 8,  8,  'java')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "INJECTS")

	for _, impl := range []int64{2, 3} {
		e, ok := edges[[2]int64{4, impl}]
		if !ok {
			t.Fatalf("missing candidate INJECTS ItemsController -> impl id %d", impl)
		}
		if e.Confidence != 0.4 {
			t.Errorf("candidate conf=%.2f, want 0.4", e.Confidence)
		}
		if e.CandidateCount != 2 {
			t.Errorf("candidate_count=%d, want 2", e.CandidateCount)
		}
		m := wiringMeta(t, e)
		if m["ambiguous"] != true {
			t.Errorf("candidate metadata = %v, want ambiguous=true", m)
		}
	}
	// The interface itself is NOT emitted alongside its candidates.
	if _, ok := edges[[2]int64{4, 1}]; ok {
		t.Error("INJECTS to the interface emitted alongside impl candidates — must not double-bind")
	}
	if len(edges) != 2 {
		t.Errorf("got %d INJECTS edges, want exactly 2 candidates", len(edges))
	}
}

// TestInjectsNestCtor covers NestJS constructor parameter-property injection:
// `constructor(private readonly svc: CatsService)` inside an @Injectable/@
// Controller class binds consumer -> provider at 0.9 / nest_ctor. A ctor in
// an UNDECORATED class must not bind (the decorator gate).
func TestInjectsNestCtor(t *testing.T) {
	src := `import { Controller, Injectable } from '@nestjs/common';

@Injectable()
export class CatsService {}

@Controller('cats')
export class CatsController {
  constructor(private readonly svc: CatsService) {}
}

export class PlainClass {
  constructor(private readonly svc: CatsService) {}
}
`
	db, files, root := routeFixture(t,
		map[string]string{"cats.ts": src},
		map[string]string{"cats.ts": "typescript"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Class', 'CatsService',    'cats.ts', 4, 4,  'typescript'),
		(2, 'Class', 'CatsController', 'cats.ts', 7, 9,  'typescript'),
		(3, 'Class', 'PlainClass',     'cats.ts', 11, 13, 'typescript')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "INJECTS")

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing INJECTS CatsController -> CatsService")
	}
	if e.ResolutionMethod != "nest_ctor" || e.Confidence != 0.9 {
		t.Errorf("method=%q conf=%.2f, want nest_ctor/0.9", e.ResolutionMethod, e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["mechanism"] != "nest_ctor" || m["declared_type"] != "CatsService" || m["resolved_to"] != "CatsService" {
		t.Errorf("metadata = %v, want mechanism=nest_ctor declared_type=CatsService resolved_to=CatsService", m)
	}

	// PlainClass is not decorated — its ctor param mints no edge.
	if _, ok := edges[[2]int64{3, 1}]; ok {
		t.Error("undecorated class ctor minted INJECTS — the decorator gate is broken")
	}
}

// TestInjectsFastAPIDepends covers `def handler(x = Depends(provider))`: the
// handler -> provider-function INJECTS edge carries fastapi_depends metadata.
func TestInjectsFastAPIDepends(t *testing.T) {
	src := `def get_db():
    yield None

def read_items(db = Depends(get_db)):
    return []
`
	db, files, root := routeFixture(t,
		map[string]string{"app.py": src},
		map[string]string{"app.py": "python"})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Function', 'get_db',     'app.py', 1, 2, 'python'),
		(2, 'Function', 'read_items', 'app.py', 4, 5, 'python')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "INJECTS")

	e, ok := edges[[2]int64{2, 1}]
	if !ok {
		t.Fatal("missing INJECTS read_items -> get_db")
	}
	if e.ResolutionMethod != "depends_injection" || e.Confidence != 0.85 {
		t.Errorf("method=%q conf=%.2f, want depends_injection/0.85", e.ResolutionMethod, e.Confidence)
	}
	m := wiringMeta(t, e)
	if m["mechanism"] != "fastapi_depends" || m["resolved_to"] != "get_db" {
		t.Errorf("metadata = %v, want mechanism=fastapi_depends resolved_to=get_db", m)
	}
}

// TestInjectsAbstainsOnAmbiguousType pins the honest-abstention contract:
// a declared type name that resolves to two different classes in two files
// mints NO edge — the wiring layer never guesses among same-named candidates.
func TestInjectsAbstainsOnAmbiguousType(t *testing.T) {
	controller := `public class ItemsController {
    @Autowired
    private SharedService svc;
}
`
	other := `class SharedService {}
`
	db, files, root := routeFixture(t,
		map[string]string{
			"ItemsController.java": controller,
			"a/SharedService.java": other,
			"b/SharedService.java": other,
		},
		map[string]string{
			"ItemsController.java": "java",
			"a/SharedService.java": "java",
			"b/SharedService.java": "java",
		})

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) VALUES
		(1, 'Class', 'ItemsController', 'ItemsController.java', 1, 4, 'java'),
		(2, 'Class', 'SharedService',   'a/SharedService.java', 1, 1, 'java'),
		(3, 'Class', 'SharedService',   'b/SharedService.java', 1, 1, 'java')`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	edges := queryWiringEdges(t, db, "INJECTS")
	if len(edges) != 0 {
		t.Errorf("got %d INJECTS edges, want 0 — same-named cross-file types must abstain", len(edges))
	}
}
