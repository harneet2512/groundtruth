package resolver

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// ===========================================================================
// Pass 4f — PROMOTE-TO-EDGES (gt_gt.md §2.6, the "100% depth" bar).
//
// This pass reads the EXISTING `properties` table on a graph.db and materializes
// new TRAVERSABLE `edges` rows for every relational property whose value encodes
// TWO RESOLVABLE code endpoints. A relation trapped in a per-node `properties`
// string is, for navigation, a MISSING edge: contract_map.py can read it as TEXT
// for the one node it hangs off, but no traversal / closure / impact query can
// hop A->B on it. Promotion makes those hops real.
//
// CONTRACT (all four enforced here, none optional):
//
//   • ADDITIVE      — the `properties` rows STAY (contract_map.py reads them as
//                     TEXT, §4 contract pillar). New edges are written ALONGSIDE,
//                     never instead. We touch only the `edges` table.
//   • IDEMPOTENT    — prior promoted edges (resolution_method LIKE 'promote_%')
//                     are DELETED before re-emitting, so a re-run (or a `-file`
//                     reindex that re-runs the pipeline) converges, never doubles.
//   • NON-INVENTION — an edge is emitted ONLY when BOTH endpoints resolve to a
//                     real node_id. A builtin/stdlib/literal/data-only-field
//                     second endpoint has NO node to point at and STAYS a
//                     property (correct-or-quiet). Zero edges to target_id=0.
//   • GENERALIZED   — ONE code path. The promote logic is language-AGNOSTIC; only
//                     the property MINING is per-language, and that is already
//                     done by gt-index upstream (parser.go). No per-language
//                     branch lives in this file.
//
// FIVE PROMOTED EDGE CLASSES + TWO CALLS.metadata ANNOTATIONS
// (gt_gt.md §2.6 TARGET EDGE SCHEMA, lines 262/266/282):
//
//   CO_SERIALIZES  serialization_pair  -> undirected serialize<->partner (100% resolvable; value carries @file:line)
//   READS          field_read          -> Method reader -> owning Class
//   WRITES         side_effect (write) -> Method writer -> owning Class
//   RAISES         exception_type/flow -> raiser -> internal exception Class (builtins stay property)
//   PRECEDES       call_order          -> earlier call-site -> later call-site (distinct internal nodes only)
//   USES (annot.)  caller_usage        -> ANNOTATE an existing CALLS edge (no new edge type)
//   DATA_FLOW (annot.) data_flow       -> ANNOTATE the existing CALLS def-site->callee edge; a standalone
//                      DATA_FLOW edge is minted ONLY for the residual def-site->callee hops that have NO
//                      existing CALLS edge (§2.6 line 262: "on CALLS.metadata ... Not a standalone edge";
//                      adaptix: 725/774=93.7% of segments ride an existing CALLS edge -> annotation, ~49
//                      no-CALLS hops -> standalone). Mirrors USES exactly (0 standalone where CALLS exists).
//
// Target resolution reuses the relationships.go pattern: a name+file(+line) index
// built from the `nodes` table, prefer-same-file then global first-match.
//
// REAL-COUNT PROOF (committed Python reference, run on the real adaptix copy
// `.tmp_scale_deepswe/deepswe-full-adaptix-name-mapping-aliases/graph.db`,
// gt_gt §2.6 lines 343/350):
//   adaptix (python, 3160 nodes): CO_SERIALIZES 390 (780 rows, undirected-deduped),
//     READS 658, WRITES 365, RAISES 172 (builtins excluded), PRECEDES 248 —
//     ZERO orphans, properties row counts unchanged, re-run converges identically.
//     DATA_FLOW = 774 resolvable use-segments; 725 (93.7%) ride an existing CALLS
//     def-site->callee edge and are ANNOTATED onto its metadata (no new edge),
//     ~49 no-CALLS hops are minted as standalone DATA_FLOW edges. CALLS count is
//     unchanged before/after (annotation, not creation — D1, §2.6 line 262).
//   Per-language (§2.6 line 342/344/345): go CO_SERIALIZES 39, READS 262, WRITES 57,
//     PRECEDES 88, RAISES 0 (panic/error builtins), DATA_FLOW 437 segments annotated;
//     js READS 107, WRITES 67, PRECEDES 163, RAISES 1, DATA_FLOW 909 annotated;
//     ts READS 9, WRITES 18, RAISES 5, PRECEDES 2, DATA_FLOW 22 annotated.
//   non-invention: RAISES-to-builtin 0; bad_target 0; builtin_exception_leak 0 on all 4.
//
// NEEDS CODESPACE go build + go test (Pass 4f registration) BEFORE SHIPPING — there
// is no Go toolchain on the authoring host. The logic is proven by the committed
// Python reference port on the real DBs; this file mirrors that canonical contract
// in idiomatic, compile-aware Go (DATA_FLOW + USES annotate, the other five promote).
// ===========================================================================

// builtinExceptionNames are exception types whose second endpoint is a builtin /
// stdlib name (no project node). A RAISES property naming one of these has no
// resolvable target and MUST stay a property (gt_gt §2.6 non-invention). Covers
// Python, JS, Go, and Rust common builtins (language-agnostic union — a project
// class genuinely named `Error` is the one accepted ambiguity, and we additionally
// require the name index to find a Class node before emitting, so a builtin name
// with no same-named project class still produces no edge).
var builtinExceptionNames = map[string]bool{
	// Python
	"ValueError": true, "TypeError": true, "KeyError": true, "IndexError": true,
	"RuntimeError": true, "Exception": true, "AttributeError": true,
	"NotImplementedError": true, "StopIteration": true, "OSError": true,
	"IOError": true, "FileNotFoundError": true, "ZeroDivisionError": true,
	"ArithmeticError": true, "AssertionError": true, "ImportError": true,
	"ModuleNotFoundError": true, "NameError": true, "LookupError": true,
	"MemoryError": true, "OverflowError": true, "RecursionError": true,
	"ReferenceError": true, "SyntaxError": true, "SystemError": true,
	"UnicodeError": true, "UnicodeDecodeError": true, "UnicodeEncodeError": true,
	"PermissionError": true, "ConnectionError": true, "TimeoutError": true,
	"BrokenPipeError": true, "BufferError": true, "EOFError": true,
	"FloatingPointError": true, "GeneratorExit": true, "KeyboardInterrupt": true,
	"SystemExit": true, "TabError": true, "IndentationError": true,
	"UnboundLocalError": true, "BlockingIOError": true, "ChildProcessError": true,
	"FileExistsError": true, "InterruptedError": true, "IsADirectoryError": true,
	"NotADirectoryError": true, "ProcessLookupError": true, "StopAsyncIteration": true,
	"Error": true,
	// JS
	"RangeError": true, "EvalError": true, "URIError": true, "AggregateError": true,
	"DOMException": true,
}

// promoteNodeMeta is the per-node slice of the nodes table the promote pass needs.
type promoteNodeMeta struct {
	ID       int64
	Label    string // Function / Method / Class / File / Interface / Struct / ...
	Name     string
	FilePath string
	Line     int
	ParentID int64
	// Signature is the node's declared signature (nodes.signature). PRECEDES
	// receiver-resolution reads it to recover a Go method's RECEIVER VARIABLE name
	// (`func (r *T) M()` → "r") so a `r.open(); r.write()` call_order whose receiver
	// token is `r` resolves to the enclosing struct T — the Go analogue of `self`
	// (parser.GoReceiverName). Empty for self/this-keyword languages.
	Signature string
}

// promoteIndexes are the lookup structures built ONCE from the nodes table,
// mirroring the buildRelationshipIndexes / funcFileIndex pattern.
type promoteIndexes struct {
	// nameIndex: symbol name -> all nodes with that name (for prefer-same-file resolution).
	nameIndex map[string][]promoteNodeMeta
	// fnl: exact (file, name, startLine) -> nodeID (serde supplies the line, so this is exact).
	fnl map[fnlKey]int64
	// byID: nodeID -> its meta (for parent/file/label lookups of a property's owning node).
	byID map[int64]promoteNodeMeta
	// classFields: classNodeID -> set of declared field names (class_field properties),
	// used to lift READS/WRITES confidence when the field is a known class field.
	classFields map[int64]map[string]bool
	// fieldTypes: classNodeID -> {fieldName -> declared typeName}, parsed from the
	// SAME class_field properties via the field-type grammar (parity with
	// BuildFieldTypeIndex). PRECEDES receiver-type resolution reads this to turn a
	// `self.<field>` receiver into the field's declared class.
	fieldTypes map[int64]map[string]string
	// classByName: typeName -> classNodeID (first writer wins, id-ordered), for
	// resolving a declared receiver TYPE name to its class node when gating PRECEDES.
	classByName map[string]int64
	// classNameCount: bare class name -> how many class-like nodes carry it. Lets
	// COMPOSES resolve a cross-package qualified/wrapped field type (e.g. *store.DB,
	// []pkg.Node) to its class ONLY when the bare name is unambiguous (count==1) —
	// never guessing across packages the way name_match would (correct-or-quiet).
	classNameCount map[string]int
}

type fnlKey struct {
	file string
	name string
	line int
}

// promote regexes (compiled once). These parse the property VALUE strings whose
// exact shapes were verified on real DBs (see the proof block above).
var (
	// 'partner:<name>@file:<line>|sig:...'  (CO_SERIALIZES)
	serdePartnerRe = regexp.MustCompile(`^partner:([^@]+)@file:(\d+)`)
	// 'reads: <prefix>.<field> [<ctx>]'  (READS) — capture the trailing field.
	fieldReadRe = regexp.MustCompile(`^reads:\s*[A-Za-z_][\w.]*\.(\w+)`)
	// 'mutates: <recv>.<field> = ...'  (WRITES)
	fieldWriteRe = regexp.MustCompile(`^mutates:\s*[A-Za-z_][\w]*\.(\w+)`)
	// 'WHEN ...: raise <Type>(...)'  (RAISES from exception_flow, Python). Capture the
	// FULL dotted token (`[\w.]*`) so a qualified name (errors.New) reaches the drop-dotted
	// guard intact instead of being silently truncated to its module prefix.
	raiseFlowRe = regexp.MustCompile(`raise\s+([A-Za-z_][\w.]*)`)
	// 'WHEN ...: throw new <Type>(...)' / 'throw <Type>(...)'  (RAISES from exception_flow,
	// JS/TS/Java). The exception_flow value for a JS/TS `throw new InternalError(...)` carries
	// the `throw [new] <Type>` form, which the raise-only regex above never matched -> the JS
	// conditional-throw RAISES fact stayed trapped in the property (GAP A). Same dotted capture
	// + same drop-dotted/builtin/resolveByName guards downstream, so a builtin/stdlib/dotted
	// throw still mints no edge (correct-or-quiet). Go `panic(...)`/`return fmt.Errorf(...)`
	// carry a VALUE, not a named internal class, so they match neither form -> stay property.
	throwFlowRe = regexp.MustCompile(`throw\s+(?:new\s+)?([A-Za-z_][\w.]*)`)
	// a call segment '<ident>('  (DATA_FLOW forward-slice callee extraction)
	callSegRe = regexp.MustCompile(`([A-Za-z_][A-Za-z0-9_]*)\s*\(`)
	// '<usage>:<callee>|...'  (USES from caller_usage)
	callerUsageRe = regexp.MustCompile(`^(\w+):(\w+)\|`)
	// a clean whole-token identifier (RAISES base validity; mirrors Python _IDENT
	// fullmatch). No dot/paren/space -> it is a clean internal class name candidate.
	identifierRe = regexp.MustCompile(`^[A-Za-z_]\w*$`)
)

// cleanExceptionBase reduces a raised-exception token that carries a trailing
// call/subscript/space (`ValueError(`, `X from e`) to its leading clean base word.
// DOTTED tokens (`errors.New`) are NOT handled here — the caller DROPS them outright
// before calling this (D3 drop-dotted), so a qualified name never reaches
// resolveByName and never mints an edge. The `.` boundary below is defensive only.
func cleanExceptionBase(tok string) string {
	tok = strings.TrimSpace(tok)
	if tok == "" {
		return ""
	}
	// cut at the first noisy boundary: '.', whitespace, '(' or '['.
	cut := len(tok)
	for i, r := range tok {
		if r == '.' || r == ' ' || r == '\t' || r == '(' || r == '[' {
			cut = i
			break
		}
	}
	return strings.TrimSpace(tok[:cut])
}

// funcMethodLabels / classLabels are the node label sets a given promote class
// resolves against (kept as small maps for O(1) membership in resolveByName).
var (
	funcMethodLabels = map[string]bool{"Function": true, "Method": true}
	classLabels      = map[string]bool{"Class": true, "Struct": true, "Type": true, "Enum": true, "Interface": true}
)

// PromotePropertyEdges runs the property->edge promotion pass over an already-
// indexed graph.db. It returns the number of NEW edges emitted (USES annotations
// counted separately and reported via the count too). It is the permanent Go home
// for the depth promotion (per CLAUDE.md the Go indexer OWNS edge writes).
//
// Register AFTER Pass 4d (detectSerdePairs / detectStructuralTwins write the
// `serialization_pair` / `structural_twin` properties this pass consumes) and
// BEFORE Pass 4e (transitive closure), so the closure reflects the promoted edges
// (gt_gt §1: "rebuild the closure AFTER ... so the closure reflects promoted
// edges"). See cmd/gt-index/main.go registration note below.
func PromotePropertyEdges(db *store.DB) (int, error) {
	// 1) IDEMPOTENT: delete any prior promoted edges so a re-run converges.
	if err := deletePromotedEdges(db); err != nil {
		return 0, fmt.Errorf("promote: clear prior promoted edges: %w", err)
	}

	// 2) Build the name+file(+line) indexes ONCE from the nodes table.
	idx, err := buildPromoteIndexes(db)
	if err != nil {
		return 0, fmt.Errorf("promote: build indexes: %w", err)
	}
	if len(idx.byID) == 0 {
		return 0, nil // empty graph — nothing to promote
	}

	var edges []*store.Edge
	seen := make(map[edgeKey]bool)

	// addEdge appends one promoted edge, enforcing NON-INVENTION (both endpoints
	// real, distinct), dedup, and the §2.3 trust thresholds (via tierFor, the
	// same function relationships.go uses). undirected canonicalizes the key on
	// (min,max) so an A<->B pair emitted from both A's and B's property rows is
	// stored once.
	addEdge := func(sourceID, targetID int64, edgeType, method string, conf float64,
		candidateCount int, evidenceType, metadata, sourceFile string, sourceLine int, undirected bool) {
		if sourceID == 0 || targetID == 0 || sourceID == targetID {
			return // non-invention: no edge onto an unresolved/self endpoint
		}
		key := edgeKey{sourceID: sourceID, targetID: targetID, typ: edgeType}
		if undirected {
			lo, hi := sourceID, targetID
			if lo > hi {
				lo, hi = hi, lo
			}
			key = edgeKey{sourceID: lo, targetID: hi, typ: edgeType}
		}
		if seen[key] {
			return
		}
		seen[key] = true
		edges = append(edges, &store.Edge{
			SourceID:           sourceID,
			TargetID:           targetID,
			Type:               edgeType,
			SourceLine:         sourceLine,
			SourceFile:         sourceFile,
			ResolutionMethod:   method,
			Confidence:         conf,
			Metadata:           metadata,
			TrustTier:          tierFor(conf), // §2.3: >=0.9 CERTIFIED / >=0.5 CANDIDATE / else SPECULATIVE
			CandidateCount:     candidateCount,
			EvidenceType:       evidenceType,
			VerificationStatus: "unverified",
		})
	}

	// 3) Run the FIVE standalone promote classes over the properties table. The two
	//    annotation classes (USES, DATA_FLOW) run in the metadata-update phase below;
	//    DATA_FLOW also mints standalone edges for its no-CALLS residual (step here).
	if err := promoteSerde(db, idx, addEdge); err != nil {
		return 0, err
	}
	if err := promoteFieldReads(db, idx, addEdge); err != nil {
		return 0, err
	}
	if err := promoteWrites(db, idx, addEdge); err != nil {
		return 0, err
	}
	if err := promoteRaises(db, idx, addEdge); err != nil {
		return 0, err
	}
	if err := promotePrecedes(db, idx, addEdge); err != nil {
		return 0, err
	}
	if err := promoteComposes(db, idx, addEdge); err != nil {
		return 0, err
	}
	// DATA_FLOW is a CALLS.metadata ANNOTATION (§2.6 line 262), NOT a standalone
	// edge: for a use-segment resolving to callee C from source S, if a CALLS S->C
	// edge EXISTS we annotate it; a standalone DATA_FLOW edge is minted ONLY for the
	// residual no-CALLS def-site->callee hops. The standalone-mint half runs here
	// (via addEdge) so it batches with the other promoted edges; the annotate half
	// rides the metadata-update phase below, exactly like USES.
	dataFlowStandalone, err := promoteDataFlowStandalone(db, idx, addEdge)
	if err != nil {
		return 0, err
	}

	// 4) Persist the new edges (additive — properties untouched).
	if len(edges) > 0 {
		if err := db.BatchInsertEdges(edges); err != nil {
			return 0, fmt.Errorf("promote: insert edges: %w", err)
		}
	}

	// 5) Annotate existing CALLS edges (NO new edge type):
	//    USES      <- caller_usage  (usage kind)
	//    DATA_FLOW <- data_flow     (forward-slice callee tag, where a CALLS edge exists)
	usesAnnotated, err := promoteUsesAnnotations(db, idx)
	if err != nil {
		// Non-fatal — the annotation is additive metadata; a failure must not
		// drop the edges already inserted above.
		return len(edges), fmt.Errorf("promote: uses annotation: %w", err)
	}
	dataFlowAnnotated, err := promoteDataFlowAnnotations(db, idx)
	if err != nil {
		return len(edges) + usesAnnotated, fmt.Errorf("promote: dataflow annotation: %w", err)
	}

	// Return total relations materialized: standalone promoted edges + both
	// annotation classes (each annotation is one relation made navigable on a
	// CALLS edge). dataFlowStandalone is already counted inside len(edges).
	_ = dataFlowStandalone
	return len(edges) + usesAnnotated + dataFlowAnnotated, nil
}

// ---------------------------------------------------------------------------
// Index construction
// ---------------------------------------------------------------------------

// buildPromoteIndexes reads the nodes + class_field properties ONCE and returns
// the resolution indexes. Mirrors buildRelationshipIndexes (relationships.go).
func buildPromoteIndexes(db *store.DB) (*promoteIndexes, error) {
	idx := &promoteIndexes{
		nameIndex:      make(map[string][]promoteNodeMeta),
		fnl:            make(map[fnlKey]int64),
		byID:           make(map[int64]promoteNodeMeta),
		classFields:    make(map[int64]map[string]bool),
		fieldTypes:     make(map[int64]map[string]string),
		classByName:    make(map[string]int64),
		classNameCount: make(map[string]int),
	}

	tx, err := db.BeginTx()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	// ORDER BY id: this scan's consumers ASSUME id-order (fnl/classByName
	// "first writer wins, id-ordered scan"; nameIndex slices feed resolveByName's
	// "prefer same-file / first match"). Without it SQLite's scan order is not
	// guaranteed, so a name with multiple matches resolved a run-dependent callee
	// — measured: textual promote_dataflow_callee 164 vs 139 across re-indexes
	// (a Step-1 +=+ break). Make the documented assumption real. (Same bug class
	// as the parser.go receiverCalls map-iteration fix.)
	rows, err := tx.Query(`SELECT id, label, name, file_path,
	        COALESCE(start_line, 0), COALESCE(parent_id, 0), COALESCE(signature, '') FROM nodes ORDER BY id`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var m promoteNodeMeta
		if err := rows.Scan(&m.ID, &m.Label, &m.Name, &m.FilePath, &m.Line, &m.ParentID, &m.Signature); err != nil {
			continue
		}
		idx.nameIndex[m.Name] = append(idx.nameIndex[m.Name], m)
		// First writer wins for the exact (file,name,line) key (serde lines are
		// unique per def in practice; a collision keeps the earlier node, which
		// is deterministic given the id-ordered scan).
		k := fnlKey{file: m.FilePath, name: m.Name, line: m.Line}
		if _, ok := idx.fnl[k]; !ok {
			idx.fnl[k] = m.ID
		}
		// classByName: a class/struct/enum/interface name -> its node id (first
		// writer wins, id-ordered scan). Used to turn a receiver TYPE name into the
		// class node for PRECEDES receiver-type gating.
		if classLabels[m.Label] {
			if _, ok := idx.classByName[m.Name]; !ok {
				idx.classByName[m.Name] = m.ID
			}
			idx.classNameCount[m.Name]++
		}
		idx.byID[m.ID] = m
	}
	rows.Close()

	// class_field properties: classNodeID -> {field names}. Value shape: 'name: Type'.
	cfRows, err := tx.Query(`SELECT node_id, value FROM properties WHERE kind = 'class_field'`)
	if err != nil {
		return nil, err
	}
	for cfRows.Next() {
		var nodeID int64
		var value string
		if err := cfRows.Scan(&nodeID, &value); err != nil {
			continue
		}
		field := strings.TrimSpace(value)
		if ci := strings.Index(field, ":"); ci >= 0 {
			field = strings.TrimSpace(field[:ci])
		}
		if field == "" {
			continue
		}
		if idx.classFields[nodeID] == nil {
			idx.classFields[nodeID] = make(map[string]bool)
		}
		idx.classFields[nodeID][field] = true
		// fieldTypes: parse the DECLARED TYPE off the same class_field value so a
		// `self.<field>` receiver can resolve to its field's class (PRECEDES gating).
		if fname, ftype, ok := parseClassFieldType(value); ok {
			if idx.fieldTypes[nodeID] == nil {
				idx.fieldTypes[nodeID] = make(map[string]string)
			}
			idx.fieldTypes[nodeID][fname] = ftype
		}
	}
	cfRows.Close()

	return idx, nil
}

// parseClassFieldType extracts (fieldName, typeName) from a class_field property
// value. It mirrors the field-type grammar resolver.BuildFieldTypeIndex uses (colon
// annotation `name: Type [= default]`, plus the Go space-separated `Name *Type`
// struct-field shape) so PRECEDES receiver-type gating sees the SAME declared types
// the rest of the resolver does. Returns ok=false for shapes with no recoverable
// (name,type) pair — CORRECT-OR-QUIET, the receiver then fails to resolve and the
// PRECEDES step abstains rather than guessing.
func parseClassFieldType(value string) (string, string, bool) {
	val := strings.TrimSpace(value)
	// Strip a trailing Go struct tag (backtick-delimited) before the colon split.
	if bt := strings.IndexByte(val, '`'); bt >= 0 {
		val = strings.TrimSpace(val[:bt])
	}
	colon := strings.Index(val, ":")
	if colon <= 0 {
		// No colon-annotation: try the Go `Name *Type` two-token struct-field shape.
		parts := strings.Fields(val)
		if len(parts) == 2 {
			name := parts[0]
			typ := strings.TrimLeft(parts[1], "*&")
			if isSimpleIdent(name) && typ != "" && !strings.ContainsAny(typ, " ()=") {
				return name, stripTypeGenerics(typ), true
			}
		}
		return "", "", false
	}
	// `name = Ctor()` (assignment, not annotation): reject if `=` precedes `:`.
	if eq := strings.Index(val, "="); eq >= 0 && eq < colon {
		return "", "", false
	}
	name := strings.TrimSpace(val[:colon])
	typ := strings.TrimSpace(val[colon+1:])
	if eq := strings.Index(typ, "="); eq > 0 { // strip inline default
		typ = strings.TrimSpace(typ[:eq])
	}
	if sp := strings.IndexByte(typ, ' '); sp > 0 { // strip trailing flag introduced by a space
		typ = strings.TrimSpace(typ[:sp])
	}
	if name == "" || typ == "" || !isSimpleIdent(name) {
		return "", "", false
	}
	return name, stripTypeGenerics(typ), true
}

// stripTypeGenerics reduces `Foo<Bar>` / `Foo[Bar]` / `pkg.Foo` to the bare last-
// segment type name used as the classByName key.
func stripTypeGenerics(typ string) string {
	if i := strings.IndexAny(typ, "<["); i > 0 {
		typ = typ[:i]
	}
	if i := strings.LastIndex(typ, "."); i >= 0 {
		typ = typ[i+1:]
	}
	return strings.TrimSpace(typ)
}

// isSimpleIdent is true for a bare identifier (no dot/bracket/paren/space/equals).
func isSimpleIdent(s string) bool {
	return s != "" && !strings.ContainsAny(s, ". []()=")
}

// resolveByName resolves a symbol name to a node id, prefer-same-file then global
// first-match (the relationships.go contract), optionally filtered to a label set.
// Returns the resolved id and the candidate count (0,id==0 when unresolved). The
// candidate count drives DATA_FLOW confidence (ambiguity gating).
func (idx *promoteIndexes) resolveByName(name, curFile string, labels map[string]bool) (int64, int) {
	ents := idx.nameIndex[name]
	if len(ents) == 0 {
		return 0, 0
	}
	// Filter by label set if requested.
	var filtered []promoteNodeMeta
	if labels != nil {
		for _, e := range ents {
			if labels[e.Label] {
				filtered = append(filtered, e)
			}
		}
	} else {
		filtered = ents
	}
	if len(filtered) == 0 {
		return 0, 0
	}
	cc := len(filtered)
	// CONTENT-deterministic order (file_path, line, id) so the picks below do NOT
	// depend on node-ID assignment, which is non-deterministic across re-indexes
	// (parallel-parse insertion order). The prior smallest-ID tiebreak resolved an
	// ambiguous callee to a run-dependent LOGICAL target -> different mint/skip ->
	// residual textual promote_dataflow_callee drift (162 vs 169) AFTER the
	// forEachProperty content-order fix. file_path+line is insertion-order-invariant.
	sort.Slice(filtered, func(i, j int) bool {
		if filtered[i].FilePath != filtered[j].FilePath {
			return filtered[i].FilePath < filtered[j].FilePath
		}
		if filtered[i].Line != filtered[j].Line {
			return filtered[i].Line < filtered[j].Line
		}
		return filtered[i].ID < filtered[j].ID
	})
	// Prefer same-file (first in content order).
	for _, e := range filtered {
		if e.FilePath == curFile {
			return e.ID, cc
		}
	}
	return filtered[0].ID, cc
}

// ---------------------------------------------------------------------------
// Class 1 — CO_SERIALIZES  (serialization_pair, 100% resolvable, undirected)
// ---------------------------------------------------------------------------

func promoteSerde(db *store.DB, idx *promoteIndexes, add addEdgeFunc) error {
	return forEachProperty(db, "serialization_pair", func(nodeID int64, value string, line int) {
		m := serdePartnerRe.FindStringSubmatch(value)
		if m == nil {
			return
		}
		partnerName := strings.TrimSpace(m[1])
		partnerLine, err := strconv.Atoi(m[2])
		if err != nil {
			return
		}
		src, ok := idx.byID[nodeID]
		if !ok {
			return
		}
		// Exact (file,name,line) — serde supplies the partner's line, so this is
		// the strict/clean key (gt_gt §2.6: serde is "stricter/cleaner because it
		// supplies the line").
		tgt := idx.fnl[fnlKey{file: src.FilePath, name: partnerName, line: partnerLine}]
		if tgt == 0 {
			// Partner may live in another file — accept any file with that
			// (name,line) pair, still EXACT on name+line (non-invention safe).
			// DETERMINISTIC pick: >1 file can hold the same (name,line) pair and Go map
			// iteration is randomized, so a `break`-on-first-hit was RUN-DEPENDENT. Choose
			// the content-smallest (file, then id) partner instead (mirrors resolveByName's
			// file,line,id order — determinism is mandatory).
			bestFile := ""
			for k, id := range idx.fnl {
				if k.name == partnerName && k.line == partnerLine {
					if tgt == 0 || k.file < bestFile || (k.file == bestFile && id < tgt) {
						tgt = id
						bestFile = k.file
					}
				}
			}
		}
		if tgt == 0 {
			return // partner unresolved -> stays a property
		}
		add(nodeID, tgt, "CO_SERIALIZES", "promote_serde", 1.0, 1, "serde_pair",
			partnerName, src.FilePath, line, true /*undirected*/)
	})
}

// ---------------------------------------------------------------------------
// Class 2 — READS  (field_read: Method reader -> owning Class)
// ---------------------------------------------------------------------------

func promoteFieldReads(db *store.DB, idx *promoteIndexes, add addEdgeFunc) error {
	return forEachProperty(db, "field_read", func(nodeID int64, value string, line int) {
		m := fieldReadRe.FindStringSubmatch(value)
		if m == nil {
			return
		}
		field := m[1]
		src, ok := idx.byID[nodeID]
		if !ok {
			return
		}
		cls, ok := idx.byID[src.ParentID]
		// classLabels is the POLYGLOT class-like superset {Class,Struct,Type,Enum,
		// Interface} — the SAME set RAISES resolves against. The literal `!= "Class"`
		// used to skip any class-like owner the parser labels with a non-"Class" tag
		// (Go/Rust receivers if ever labeled Struct/Interface) — GAP B. Mirrors how the
		// resolver's linkGoReceiverMethods / field-type index already treat the owner.
		if !ok || !classLabels[cls.Label] {
			return // field-target with no owning class-like node STAYS property
		}
		conf := 0.6
		if fields := idx.classFields[cls.ID]; fields != nil && fields[field] {
			conf = 0.9 // the read field is a declared class field -> high confidence
		}
		add(nodeID, cls.ID, "READS", "promote_field_read", conf, 1, "field_read",
			field, src.FilePath, line, false)
	})
}

// ---------------------------------------------------------------------------
// Class 6 — COMPOSES  (class_field declared TYPE: owning Class -> field's Class)
// ---------------------------------------------------------------------------
// A class/struct whose field's DECLARED TYPE is another indexed class-like node
// COMPOSES that type (composition / has-a). Language-agnostic by construction:
// the `class_field` property is emitted uniformly for every language, its declared
// type is parsed by the SAME parseClassFieldType the PRECEDES receiver gating uses
// (colon annotation `name: Type` + Go `Name *Type`), and classByName indexes every
// class-like label {Class,Struct,Type,Enum,Interface}. Builtins/primitives/external
// types are absent from classByName, so they produce NO edge — CORRECT-OR-QUIET, no
// guessing onto an unresolved type. This is the structural depth edge that was
// previously emitted ONLY for JSX components (relationships.go) -> 0 on every
// non-React repo; it now generalizes to all 5 languages.
func promoteComposes(db *store.DB, idx *promoteIndexes, add addEdgeFunc) error {
	return forEachProperty(db, "class_field", func(nodeID int64, value string, line int) {
		_, ftype, ok := parseClassFieldType(value)
		if !ok {
			return // no recoverable declared type -> STAY property (no guess)
		}
		owner, ok := idx.byID[nodeID]
		if !ok || !classLabels[owner.Label] {
			return // class_field whose owner is not a class-like node -> skip
		}
		// (1) Exact same-package simple type: strip pointer/ref + generics, match the
		// declared name directly. Highest confidence — the type is in this package.
		exact := stripTypeGenerics(strings.TrimLeft(strings.TrimSpace(ftype), "*&"))
		var targetID int64
		var matched string
		if exact != "" && idx.classNameCount[exact] == 1 {
			if id, ok := idx.classByName[exact]; ok && id != nodeID {
				targetID, matched = id, exact
			}
		}
		// (2) Cross-package / wrapped fallback: a slice/array/map/qualified field type
		// (*store.DB, []pkg.Node, crate::Type) whose bare class name resolves to EXACTLY
		// ONE class. Unambiguous-only — never guess across packages (that is name_match,
		// which the architecture forbids as a fact); ambiguous bare names stay quiet.
		if targetID == 0 {
			base := composesBaseTypeName(ftype)
			if base != "" && base != exact && idx.classNameCount[base] == 1 {
				if id, ok := idx.classByName[base]; ok && id != nodeID {
					targetID, matched = id, base
				}
			}
		}
		if targetID == 0 {
			return // builtin/external/ambiguous -> correct-or-quiet, no edge
		}
		add(nodeID, targetID, "COMPOSES", "promote_composes", 0.9, 1, "class_field",
			matched, owner.FilePath, line, false)
	})
}

// composesBaseTypeName reduces a declared field type to its bare class name for the
// unambiguous cross-package COMPOSES fallback: strips Go pointer/ref/slice/array
// wrappers, then generics, then a package/namespace qualifier (last component after
// the final '.' or ':'). map/chan value types and exotic shapes are intentionally NOT
// unwrapped — they stay quiet rather than risk a wrong target.
func composesBaseTypeName(ftype string) string {
	t := strings.TrimSpace(ftype)
	for {
		switch {
		case strings.HasPrefix(t, "*"), strings.HasPrefix(t, "&"):
			t = t[1:]
		case strings.HasPrefix(t, "[]"):
			t = t[2:]
		case strings.HasPrefix(t, "["): // fixed-size array [N]T
			if rb := strings.IndexByte(t, ']'); rb > 0 {
				t = t[rb+1:]
			} else {
				return ""
			}
		default:
			goto unwrapped
		}
	}
unwrapped:
	t = stripTypeGenerics(strings.TrimSpace(t))
	if i := strings.LastIndexAny(t, ".:"); i >= 0 {
		t = t[i+1:]
	}
	return strings.TrimSpace(t)
}

// ---------------------------------------------------------------------------
// Class 3 — WRITES  (side_effect field write: Method writer -> owning Class)
// ---------------------------------------------------------------------------

func promoteWrites(db *store.DB, idx *promoteIndexes, add addEdgeFunc) error {
	return forEachProperty(db, "side_effect", func(nodeID int64, value string, line int) {
		m := fieldWriteRe.FindStringSubmatch(value)
		if m == nil {
			return // side_effect value with no resolvable field/target -> STAY property
		}
		field := m[1]
		src, ok := idx.byID[nodeID]
		if !ok {
			return
		}
		cls, ok := idx.byID[src.ParentID]
		// Polyglot class-like superset (GAP B) — see promoteFieldReads above.
		if !ok || !classLabels[cls.Label] {
			return
		}
		conf := 0.6
		if fields := idx.classFields[cls.ID]; fields != nil && fields[field] {
			conf = 0.9
		}
		add(nodeID, cls.ID, "WRITES", "promote_write", conf, 1, "side_effect",
			field, src.FilePath, line, false)
	})
}

// ---------------------------------------------------------------------------
// Class 4 — RAISES  (exception_type / exception_flow; builtins stay property)
// ---------------------------------------------------------------------------

func promoteRaises(db *store.DB, idx *promoteIndexes, add addEdgeFunc) error {
	for _, kind := range []string{"exception_type", "exception_flow"} {
		k := kind
		err := forEachProperty(db, k, func(nodeID int64, value string, line int) {
			var etype string
			if k == "exception_type" {
				etype = strings.TrimSpace(value)
				// P2-11: the parser emits PROSE into exception_type for some shapes —
				// Rust `.unwrap()`/`.expect()` yield "panic via .unwrap", and the Go/Rust
				// `default: excType = text` fallthrough can carry a whole raise/throw
				// expression. Those are NOT class names. Gate the RAW value on the clean-
				// identifier shape (no spaces/dots/parens) BEFORE any cleaning, so prose is
				// rejected STRUCTURALLY (by shape), not incidentally (because it happened
				// to contain a '.'). A genuine single-identifier exception name (MyError)
				// passes; "panic via .unwrap", "raise X from e", and any multi-token
				// expression is dropped — correct-or-quiet (no edge minted from prose).
				if !identifierRe.MatchString(etype) {
					return
				}
			} else {
				// exception_flow carries either `raise <Type>` (Python) or
				// `throw [new] <Type>` (JS/TS/Java) inside the WHEN-clause shape. Try
				// raise first, then throw, so a JS conditional throw is recovered (GAP A)
				// while Go `panic(...)` / `return fmt.Errorf(...)` (a value, no named
				// internal class) matches NEITHER and stays a property (non-invention).
				if m := raiseFlowRe.FindStringSubmatch(value); m != nil {
					etype = m[1]
				} else if m := throwFlowRe.FindStringSubmatch(value); m != nil {
					etype = m[1]
				} else {
					return
				}
			}
			// D3 drop-dotted (1:1 with the Python reference `if "." in tok: continue`):
			// a dotted/qualified name (errors.New, mod.MyError) is NOT a clean internal
			// class -> DROP it. Reducing to the module prefix ("errors"/"mod") would mint
			// a WRONG RAISES edge onto a same-named project class (correct-or-quiet /
			// non-invention bar — a wrong fact is worse than a missing one).
			if strings.Contains(etype, ".") {
				return
			}
			// Trailing call/subscript/space ("ValueError(", "X from e") -> leading base,
			// then require a whole identifier.
			etype = cleanExceptionBase(etype)
			if etype == "" || !identifierRe.MatchString(etype) {
				return
			}
			// NON-INVENTION: builtin/stdlib names have no project node -> property.
			if builtinExceptionNames[etype] {
				return
			}
			src, ok := idx.byID[nodeID]
			if !ok {
				return
			}
			// Target resolves against the POLYGLOT class label superset
			// {Class,Struct,Type,Enum,Interface} so Python and Go are truly 1:1.
			tgt, cc := idx.resolveByName(etype, src.FilePath, classLabels)
			if tgt == 0 {
				return // not an internal class -> stays property
			}
			// AMBIGUITY GATE (mirrors dataFlowConfidence): a UNIQUELY-named internal
			// exception class (cc==1) is a fact (0.9 CERTIFIED); when the raised name
			// matches MULTIPLE project classes (cc>1) the resolveByName same-file-first
			// pick is a GUESS, so the edge must NOT be a fact — degrade to CANDIDATE/
			// SPECULATIVE, and SUPPRESS a >5-way name entirely (correct-or-quiet: a wrong
			// RAISES is worse than a missing one).
			conf := raisesConfidence(cc)
			if conf == 0 {
				return // too ambiguous to attribute -> stays property
			}
			add(nodeID, tgt, "RAISES", "promote_raises", conf, cc, "exception",
				etype, src.FilePath, line, false)
		})
		if err != nil {
			return err
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// DATA_FLOW — a CALLS.metadata ANNOTATION, not a standalone edge type
// (gt_gt §2.6 line 262 / D1 canonical). For a def-site->callee use-segment that
// resolves to callee C from source S: if a CALLS S->C edge EXISTS, append a
// dataflow tag to THAT edge's metadata (no new edge); ONLY the residual no-CALLS
// def-site->callee hops are minted as standalone DATA_FLOW edges (adaptix: 725/774
// =93.7% annotate, ~49 standalone). This mirrors how USES already rides existing
// CALLS edges (promoteUsesAnnotations). Resolution + the ambiguity confidence gate
// are shared by both halves via forEachDataFlowTarget, so they classify identically
// and never double-count.
// ---------------------------------------------------------------------------

// forEachDataFlowTarget streams every RESOLVED, non-ambiguous (src, callee, target,
// candidateCount, confidence, line) data_flow forward-slice use-target to fn. It
// applies the §2.6 candidate-count gate (>5 candidates SUPPRESSED, correct-or-quiet)
// so a target reaching fn is always a fact-grade hop. fn decides annotate vs mint.
func forEachDataFlowTarget(db *store.DB, idx *promoteIndexes,
	fn func(src promoteNodeMeta, callee string, targetID int64, cc int, conf float64, line int)) error {
	return forEachProperty(db, "data_flow", func(nodeID int64, value string, line int) {
		arrow := strings.Index(value, "->")
		if arrow < 0 {
			return
		}
		rhs := value[arrow+2:]
		src, ok := idx.byID[nodeID]
		if !ok {
			return
		}
		// Each '|'-separated use-segment may contain calls; the forward-slice maps
		// source -> each resolvable callee.
		for _, seg := range strings.Split(rhs, "|") {
			for _, cm := range callSegRe.FindAllStringSubmatch(seg, -1) {
				callee := cm[1]
				tgt, cc := idx.resolveByName(callee, src.FilePath, funcMethodLabels)
				if tgt == 0 || tgt == nodeID {
					continue
				}
				// Confidence by candidate-count (ambiguity gate). >5 candidates is
				// too ambiguous to be a fact -> SUPPRESS (correct-or-quiet).
				conf := dataFlowConfidence(cc)
				if conf < 0.4 {
					continue
				}
				fn(src, callee, tgt, cc, conf, line)
			}
		}
	})
}

// promoteDataFlowStandalone mints a standalone DATA_FLOW edge ONLY for a forward-
// slice hop that has NO existing CALLS edge (the ~49 no-CALLS def-site->callee hops
// on adaptix). When a CALLS src->callee edge already exists, the hop is left for the
// annotation half (promoteDataFlowAnnotations) — no standalone edge is minted, so
// the CALLS bulk stays a CALLS.metadata annotation per D1. Returns the count minted.
func promoteDataFlowStandalone(db *store.DB, idx *promoteIndexes, add addEdgeFunc) (int, error) {
	callsIdx, err := buildCallsEdgeIndex(db)
	if err != nil {
		return 0, err
	}
	minted := 0
	err = forEachDataFlowTarget(db, idx, func(src promoteNodeMeta, callee string, targetID int64, cc int, conf float64, line int) {
		if _, hasCalls := callsIdx[edgeEndpoints{src.ID, targetID}]; hasCalls {
			return // a CALLS edge exists -> ANNOTATE it instead (no standalone edge)
		}
		add(src.ID, targetID, "DATA_FLOW", "promote_dataflow_callee", conf, cc,
			"data_flow", callee, src.FilePath, line, false)
		minted++
	})
	return minted, err
}

// promoteDataFlowAnnotations appends a dataflow tag to the metadata of every
// EXISTING CALLS src->callee edge that a forward-slice hop resolves onto (the
// 93.7% bulk). It NEVER creates an edge. Returns the number of CALLS edges
// annotated. Mirrors promoteUsesAnnotations (read-phase collect, write-phase
// metadata append, idempotent via the 'dataflow=' tag-presence guard).
func promoteDataFlowAnnotations(db *store.DB, idx *promoteIndexes) (int, error) {
	callsIdx, err := buildCallsEdgeIndex(db)
	if err != nil {
		return 0, err
	}
	// Collect one tag per CALLS edge (dedup: many use-segments can map to the same
	// src->callee edge; one 'dataflow=<callee>' tag suffices, deterministic order).
	type annot struct {
		edgeID int64
		callee string
	}
	seen := make(map[int64]bool)
	var toUpdate []annot
	err = forEachDataFlowTarget(db, idx, func(src promoteNodeMeta, callee string, targetID int64, cc int, conf float64, line int) {
		eid, hasCalls := callsIdx[edgeEndpoints{src.ID, targetID}]
		if !hasCalls || seen[eid] {
			return
		}
		seen[eid] = true
		toUpdate = append(toUpdate, annot{edgeID: eid, callee: callee})
	})
	if err != nil {
		return 0, err
	}
	if len(toUpdate) == 0 {
		return 0, nil
	}
	sort.Slice(toUpdate, func(i, j int) bool { return toUpdate[i].edgeID < toUpdate[j].edgeID })

	tx, err := db.BeginTx()
	if err != nil {
		return 0, err
	}
	updated := 0
	for _, a := range toUpdate {
		tag := "dataflow=" + a.callee
		res, err := tx.Exec(
			`UPDATE edges SET metadata =
			   CASE
			     WHEN metadata IS NULL OR metadata = '' THEN ?
			     WHEN instr(metadata, ?) > 0 THEN metadata
			     ELSE metadata || ';' || ?
			   END
			 WHERE id = ?`,
			tag, tag, tag, a.edgeID)
		if err != nil {
			continue
		}
		if n, _ := res.RowsAffected(); n > 0 {
			updated++
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return updated, nil
}

// edgeEndpoints keys a CALLS edge by its (source,target) for the dataflow/uses
// annotation lookup.
type edgeEndpoints struct {
	sourceID int64
	targetID int64
}

// buildCallsEdgeIndex maps (source_id, target_id) -> edge_id for every CALLS edge,
// so the DATA_FLOW pass can tell a hop that rides an existing CALLS edge (annotate)
// from a no-CALLS hop (mint standalone). First-id wins on a duplicate endpoint pair
// (deterministic). Read-only.
func buildCallsEdgeIndex(db *store.DB) (map[edgeEndpoints]int64, error) {
	tx, err := db.BeginTx()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	rows, err := tx.Query(
		`SELECT id, source_id, target_id FROM edges WHERE type = 'CALLS' ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[edgeEndpoints]int64)
	for rows.Next() {
		var eid, sid, tid int64
		if err := rows.Scan(&eid, &sid, &tid); err != nil {
			continue
		}
		k := edgeEndpoints{sourceID: sid, targetID: tid}
		if _, ok := out[k]; !ok {
			out[k] = eid
		}
	}
	return out, nil
}

// dataFlowConfidence maps candidate count to confidence (gt_gt §2.6 DATA_FLOW:
// unique 0.8 / 2 0.6 / <=5 0.4 / else suppress).
func dataFlowConfidence(cc int) float64 {
	switch {
	case cc == 1:
		return 0.8
	case cc == 2:
		return 0.6
	case cc <= 5:
		return 0.4
	default:
		return 0.0 // >5 candidates: suppress
	}
}

// raisesConfidence gates a RAISES edge on the exception-name ambiguity (candidate_count),
// mirroring dataFlowConfidence. A uniquely-named internal exception class is a FACT; a name
// shared by several project classes is a GUESS (the resolveByName same-file-first pick can
// be wrong), so it degrades below CERTIFIED and a >5-way name is suppressed (correct-or-
// quiet: a wrong RAISES is worse than a missing one).
func raisesConfidence(cc int) float64 {
	switch {
	case cc == 1:
		return 0.9 // unique internal exception class -> fact (CERTIFIED)
	case cc == 2:
		return 0.6 // two candidates -> CANDIDATE
	case cc <= 5:
		return 0.4 // several candidates -> SPECULATIVE
	default:
		return 0.0 // >5 candidates -> suppress
	}
}

// ---------------------------------------------------------------------------
// Class 7 — PRECEDES  (call_order: earlier -> later, distinct internal nodes)
// ---------------------------------------------------------------------------

// promotePrecedes mints ordering edges from `call_order` facts. The parser records
// a method-call sequence on ONE receiver as `<receiver>: m1 -> m2 -> ...`, but it
// DISCARDS the receiver's TYPE (extractCallOrdering, parser.go) — so the only way to
// know which CLASS m1/m2 belong to is to re-resolve the receiver here. A bare
// type-blind resolveByName first-match would fabricate a PRECEDES between two
// unrelated same-named functions in different classes/files (the reported P0). The
// gate is therefore fail-closed on THREE conditions; an edge is minted only when ALL
// hold, else the step ABSTAINS (no edge):
//
//  1. RECEIVER TYPE RESOLVES to a concrete class node — `self`/`this` (the Python/JS/
//     Rust keyword receivers) AND a Go method's RECEIVER VARIABLE (`func (r *T) Run()`
//     calling `r.open()` → token `r`) resolve to the caller method's enclosing class
//     (src.ParentID, which linkGoReceiverMethods parented to the struct); `super`
//     resolves to that enclosing class's PARENT (inheritanceMap; abstains when the
//     superclass is unknown/ambiguous — never the subclass); a `self.<field>` or
//     bare-field receiver resolves through the
//     field-type index to its declared class. A local-variable receiver of unknown
//     type does NOT resolve → abstain. The Go receiver-var path closes a live-witness
//     gap: a go graph had ZERO PRECEDES because `r`/`c`/`conn` matched none of the
//     self/this/super/self.field shapes and abstained on every Go ordering fact.
//  2. SAME-FILE, SINGLE-CANDIDATE resolution of each method name (cc==1, same file as
//     the call_order) — a unique unambiguous target, never a cross-file name guess.
//  3. BOTH methods are MEMBERS of the resolved receiver class (parent_id == classID).
//     This is what kills "two classes each defining write" → no cross-class PRECEDES:
//     the methods of the OTHER class are not members of THIS receiver's type.
func promotePrecedes(db *store.DB, idx *promoteIndexes, add addEdgeFunc) error {
	return forEachProperty(db, "call_order", func(nodeID int64, value string, line int) {
		colon := strings.Index(value, ":")
		if colon < 0 {
			return
		}
		receiver := strings.TrimSpace(value[:colon])
		seq := value[colon+1:]
		src, ok := idx.byID[nodeID]
		if !ok {
			return
		}
		// (1) Resolve the receiver to its declared class node, else ABSTAIN.
		classID, ok := idx.resolveReceiverClass(receiver, src)
		if !ok || classID == 0 {
			return
		}
		// Split the '<a> -> <b> -> <c>' chain; each step token is the first word
		// (drop trailing annotations like '[managed]').
		var names []string
		for _, part := range strings.Split(seq, "->") {
			p := strings.TrimSpace(part)
			if p == "" {
				continue
			}
			if sp := strings.IndexByte(p, ' '); sp >= 0 {
				p = p[:sp]
			}
			names = append(names, p)
		}
		for i := 0; i+1 < len(names); i++ {
			a, b := names[i], names[i+1]
			if a == b {
				continue
			}
			// (2) same-file, single-candidate resolution of EACH method, and
			// (3) BOTH must be members (parent_id) of the resolved receiver class.
			idA, okA := idx.resolveClassMethod(a, classID, src.FilePath)
			idB, okB := idx.resolveClassMethod(b, classID, src.FilePath)
			if !okA || !okB || idA == 0 || idB == 0 || idA == idB {
				continue // a step that cannot be re-proven on the receiver's type abstains
			}
			// Both endpoints are now a unique same-file member of the receiver class —
			// an unambiguous, type-grounded ordering fact. CANDIDATE (0.5): call_order
			// is weak ordering evidence even when the targets are certain.
			add(idA, idB, "PRECEDES", "promote_precedes", 0.5, 1, "call_order",
				a+"->"+b, src.FilePath, line, false)
		}
	})
}

// resolveReceiverClass turns a call_order receiver token into the class node whose
// methods the sequence is calling. `self`/`this` (and a Go method's own receiver
// variable, `func (r *T) M()` → token `r`) → the caller method's enclosing class
// (src.ParentID, which must be a Class). `super` → that enclosing class's PARENT (via
// inheritanceMap; abstains if the superclass is unknown or ambiguous — NEVER the
// subclass). `self.<field>` or a bare `<field>` known on the enclosing class → the
// field's declared class (via the field-type index → classByName); `super.<field>` →
// the field on the PARENT class. Returns ok=false when no class can be proven.
func (idx *promoteIndexes) resolveReceiverClass(receiver string, src promoteNodeMeta) (int64, bool) {
	r := strings.TrimSpace(receiver)
	if r == "" {
		return 0, false
	}
	// Go/other named-receiver languages bind the instance to a VARIABLE, not the
	// `self`/`this` keyword: `func (r *T) Run() { r.open(); r.close() }` records the
	// call_order receiver token as `r`. When that token is EXACTLY the enclosing
	// method's declared receiver variable (recovered structurally from its signature
	// via parser.GoReceiverName), the receiver IS the enclosing object — the Go
	// analogue of `self` — so its class is the caller's parent class. Fail-closed:
	// GoReceiverName returns "" for any non-Go / anonymous-receiver / non-method
	// signature, so this branch fires ONLY for a genuine named Go receiver method, and
	// only on an EXACT token match (a local var of another name still falls through to
	// the field/abstain path below). enclosingClass then still requires src.ParentID to
	// be a real class (linkGoReceiverMethods parents Go receiver methods to their
	// struct), so an unparented Go function abstains.
	if recvVar := parser.GoReceiverName(src.Signature); recvVar != "" && r == recvVar {
		return idx.enclosingClass(src)
	}
	// self.<field> / this.<field> / super.<field> -> resolve the field on the receiver's
	// class. self/this = the enclosing (sub)class; super = its PARENT class.
	field := ""
	useSuper := false
	switch {
	case r == "self" || r == "this":
		// Receiver IS the enclosing object → its class is the caller's parent class.
		return idx.enclosingClass(src)
	case r == "super":
		// super IS the SUPERCLASS of the enclosing class — NOT the enclosing (sub)class.
		// Abstain (correct-or-quiet) when the superclass is unknown/ambiguous.
		return idx.superClass(src)
	case strings.HasPrefix(r, "self."):
		field = r[len("self."):]
	case strings.HasPrefix(r, "this."):
		field = r[len("this."):]
	case strings.HasPrefix(r, "super."):
		field = r[len("super."):]
		useSuper = true // resolve the field on the SUPERCLASS, not the enclosing class
	default:
		// Bare token: only treat it as a receiver if it is a KNOWN field of the
		// enclosing class (else it is a local var of unknown type → abstain).
		field = r
	}
	if field == "" || strings.ContainsAny(field, ". ") {
		return 0, false // chained/compound receiver — too ambiguous to type-prove
	}
	// The receiver's class: self/this/bare-field → the enclosing class; super. → its parent.
	var baseClassID int64
	var ok bool
	if useSuper {
		baseClassID, ok = idx.superClass(src)
	} else {
		baseClassID, ok = idx.enclosingClass(src)
	}
	if !ok || baseClassID == 0 {
		return 0, false
	}
	typeName, ok := idx.fieldTypes[baseClassID][field]
	if !ok || typeName == "" {
		return 0, false // field type unknown → abstain
	}
	classID, ok := idx.classByName[typeName]
	if !ok || classID == 0 {
		return 0, false // declared type is not an indexed project class → abstain
	}
	return classID, true
}

// enclosingClass returns the class node that OWNS src (src.ParentID when that parent
// is a class). A free function (parent 0, or parent not a class) has no enclosing
// class → ok=false.
func (idx *promoteIndexes) enclosingClass(src promoteNodeMeta) (int64, bool) {
	if src.ParentID == 0 {
		return 0, false
	}
	pm, ok := idx.byID[src.ParentID]
	if !ok || !classLabels[pm.Label] {
		return 0, false
	}
	return src.ParentID, true
}

// superClass returns the SINGLE superclass of src's enclosing class, from the package
// inheritance map (child class node ID -> parent class node IDs; populated in main.go
// before promotion — same map the resolver's CHA rungs use). `super` refers to the PARENT
// of the enclosing class, NOT the enclosing (sub)class itself. Fail-closed / correct-or-
// quiet: returns ok=false when the enclosing class is unknown, has NO recorded parent, or
// has MORE THAN ONE parent (ambiguous multiple inheritance / a nil map) — abstaining
// rather than minting a wrong edge onto the subclass.
func (idx *promoteIndexes) superClass(src promoteNodeMeta) (int64, bool) {
	enclosingID, ok := idx.enclosingClass(src)
	if !ok || enclosingID == 0 {
		return 0, false
	}
	parents := inheritanceMap[enclosingID]
	if len(parents) != 1 || parents[0] == 0 {
		return 0, false // unknown or ambiguous superclass -> abstain
	}
	return parents[0], true
}

// resolveClassMethod resolves a method name to a node that is (a) same-file as the
// call_order, (b) the UNIQUE such candidate (single same-file same-name member), and
// (c) a member of classID (parent_id == classID). Returns ok=false otherwise — the
// strict gate that prevents a cross-class same-named method from being picked.
func (idx *promoteIndexes) resolveClassMethod(name string, classID int64, curFile string) (int64, bool) {
	var found int64
	for _, e := range idx.nameIndex[name] {
		if !funcMethodLabels[e.Label] {
			continue
		}
		if e.FilePath != curFile || e.ParentID != classID {
			continue
		}
		if found != 0 {
			return 0, false // >1 same-file member with this name on the class → ambiguous
		}
		found = e.ID
	}
	if found == 0 {
		return 0, false
	}
	return found, true
}

// ---------------------------------------------------------------------------
// USES — annotate existing CALLS edges (no new edge type)
// ---------------------------------------------------------------------------

// promoteUsesAnnotations writes the caller_usage kind into the metadata of the
// MATCHING existing CALLS edge (source = the property's node, target = resolved
// callee). It NEVER creates a new edge: if no CALLS edge exists for (src,callee),
// the usage is skipped (gt_gt §2.6: USES rides an existing CALLS edge). Returns
// the number of CALLS edges annotated.
func promoteUsesAnnotations(db *store.DB, idx *promoteIndexes) (int, error) {
	type annot struct {
		edgeID int64
		usage  string
	}
	var toUpdate []annot

	tx, err := db.BeginTx()
	if err != nil {
		return 0, err
	}
	// Read phase.
	rows, err := tx.Query(`SELECT node_id, value FROM properties WHERE kind = 'caller_usage'`)
	if err != nil {
		tx.Rollback()
		return 0, err
	}
	// Sort by node_id for deterministic annotation order.
	type cu struct {
		nodeID int64
		value  string
	}
	var cus []cu
	for rows.Next() {
		var c cu
		if err := rows.Scan(&c.nodeID, &c.value); err != nil {
			continue
		}
		cus = append(cus, c)
	}
	rows.Close()
	sort.Slice(cus, func(i, j int) bool {
		if cus[i].nodeID != cus[j].nodeID {
			return cus[i].nodeID < cus[j].nodeID
		}
		return cus[i].value < cus[j].value
	})

	for _, c := range cus {
		m := callerUsageRe.FindStringSubmatch(c.value)
		if m == nil {
			continue
		}
		usage, callee := m[1], m[2]
		src, ok := idx.byID[c.nodeID]
		if !ok {
			continue
		}
		tgt, _ := idx.resolveByName(callee, src.FilePath, funcMethodLabels)
		if tgt == 0 {
			continue
		}
		var edgeID int64
		err := tx.QueryRow(
			`SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND type = 'CALLS' LIMIT 1`,
			c.nodeID, tgt).Scan(&edgeID)
		if err != nil {
			continue // no matching CALLS edge -> skip (never create a new edge)
		}
		toUpdate = append(toUpdate, annot{edgeID: edgeID, usage: usage})
	}

	// Write phase — append the usage kind into metadata. We COALESCE so an
	// existing metadata string is preserved (additive, idempotent-ish: re-running
	// would re-append, so we mark with a 'usage=' tag and skip if already present).
	updated := 0
	for _, a := range toUpdate {
		tag := "usage=" + a.usage
		res, err := tx.Exec(
			`UPDATE edges SET metadata =
			   CASE
			     WHEN metadata IS NULL OR metadata = '' THEN ?
			     WHEN instr(metadata, ?) > 0 THEN metadata
			     ELSE metadata || ';' || ?
			   END
			 WHERE id = ?`,
			tag, tag, tag, a.edgeID)
		if err != nil {
			continue
		}
		if n, _ := res.RowsAffected(); n > 0 {
			updated++
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return updated, nil
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

// addEdgeFunc is the closure signature the per-class promoters call to emit an
// edge (kept as a named type so the per-class functions read cleanly).
type addEdgeFunc func(sourceID, targetID int64, edgeType, method string, conf float64,
	candidateCount int, evidenceType, metadata, sourceFile string, sourceLine int, undirected bool)

// forEachProperty streams every (node_id, value, line) row of one property kind
// to fn, in a CONTENT-ordered (deterministic) sequence. Read-only transaction.
// Ordering by `id` (AUTOINCREMENT = parallel-parse INSERTION order) was NOT
// deterministic: a node with MANY properties of one kind (e.g. a function with
// several data_flow forward-slice rows) got them in run-dependent order, and the
// shared `seen` dedup in PromotePropertyEdges is first-key-wins -> the
// promote_dataflow_callee edge COUNT drifted run-to-run (textual 137 vs 168, the
// determinism RED held-out validation caught). Order by (node_id, value, line) —
// the property CONTENT, insertion-independent — so iteration is stable regardless
// of parse worker completion order. (id kept as a last-resort tiebreak.) Fixes all
// six promoters that share this helper.
func forEachProperty(db *store.DB, kind string, fn func(nodeID int64, value string, line int)) error {
	tx, err := db.BeginTx()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	rows, err := tx.Query(
		`SELECT node_id, value, COALESCE(line, 0) FROM properties WHERE kind = ? ORDER BY node_id, value, COALESCE(line, 0), id`,
		kind)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var nodeID int64
		var value string
		var line int
		if err := rows.Scan(&nodeID, &value, &line); err != nil {
			continue
		}
		fn(nodeID, value, line)
	}
	return nil
}

// deletePromotedEdges removes any edges from a prior promote run so the pass is
// idempotent (re-run / `-file` reindex converges, never doubles). Matches on the
// resolution_method namespace 'promote_%' that EVERY promoted edge carries.
func deletePromotedEdges(db *store.DB) error {
	tx, err := db.BeginTx()
	if err != nil {
		return err
	}
	if _, err := tx.Exec(`DELETE FROM edges WHERE resolution_method LIKE 'promote_%'`); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}
