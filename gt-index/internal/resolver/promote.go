package resolver

import (
	"database/sql"
	"encoding/json"
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
// READS/WRITES additionally carry a statement-level substrate in the additive
// `edges.access_sites` column (JSON): the field (still == metadata, byte-exact),
// the access kind, the primary access line (== source_line), the enclosing
// method's node id + published stable id when one exists, and `sites` — every
// (field,line) access row edge-dedup collapsed into the single edge. This is the
// producer-side surface a statement-level consumer (a def-site->use-site join)
// reads; `metadata` itself is untouched.
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
	// classByName: typeName -> classNodeID — the CONTENT-smallest (file_path,
	// start_line, id) class carrying the name, NOT first-writer on the id-ordered
	// scan: a batch amend re-enters the edited file's nodes at the top of the
	// AUTOINCREMENT space, so smallest-id names a different class under an amend
	// than under a full rebuild. Used to turn a receiver TYPE name into the class
	// node for PRECEDES receiver-type gating.
	classByName map[string]int64
	// stableIDs: nodeID -> the node's published stable identity, when one exists
	// (nodes.stable_id, else the resolution_symbols sidecar keyed by
	// native_id = nodes.id — the same fallback join process.go readStableIDs
	// uses; on a real graph parsed symbol nodes carry stable_id NULL while
	// resolution_symbols holds it). READS/WRITES access-site metadata stamps it
	// as the enclosing method's rebuild-invariant scope identity
	// (scope_stable_id); absent -> the key is omitted, never fabricated.
	stableIDs map[int64]string
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
	// 'reads: <recv>.<field> [<ctx>]'  (READS) — capture receiver chain AND the
	// trailing field so the access-site payload can carry receiver identity.
	fieldReadRe = regexp.MustCompile(`^reads:\s*([A-Za-z_][\w.]*)\.(\w+)`)
	// 'mutates: <recv>.<field> = ...'  (WRITES)
	fieldWriteRe = regexp.MustCompile(`^mutates:\s*([A-Za-z_][\w]*)\.(\w+)`)
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
	// READS/WRITES additionally return their per-edge access-site payloads (the
	// statement-level rows edge dedup collapsed); keyed by the same edgeKey.
	readSites, err := promoteFieldReads(db, idx, addEdge)
	if err != nil {
		return 0, err
	}
	writeSites, err := promoteWrites(db, idx, addEdge)
	if err != nil {
		return 0, err
	}
	if err := promoteRaises(db, idx, addEdge); err != nil {
		return 0, err
	}
	if err := promotePrecedes(db, idx, addEdge); err != nil {
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

	// Stamp the access-site payloads onto the minted READS/WRITES edges. The map
	// key is the dedup edgeKey, so an entry applies only to an edge that was
	// actually emitted; a payload whose edge never minted (non-invention drop)
	// simply goes unused.
	for _, e := range edges {
		var payload string
		switch e.Type {
		case "READS":
			payload = readSites[edgeKey{sourceID: e.SourceID, targetID: e.TargetID, typ: e.Type}]
		case "WRITES":
			payload = writeSites[edgeKey{sourceID: e.SourceID, targetID: e.TargetID, typ: e.Type}]
		}
		if payload != "" {
			e.AccessSites = payload
		}
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
		nameIndex:   make(map[string][]promoteNodeMeta),
		fnl:         make(map[fnlKey]int64),
		byID:        make(map[int64]promoteNodeMeta),
		classFields: make(map[int64]map[string]bool),
		fieldTypes:  make(map[int64]map[string]string),
		classByName: make(map[string]int64),
		stableIDs:   make(map[int64]string),
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
		// classByName: a class/struct/enum/interface name -> its node id. Keep the
		// CONTENT-smallest (file_path, start_line, id) candidate — first-writer on
		// this id-ordered scan is not stable: a batch amend re-inserts the edited
		// file's nodes at the top of the AUTOINCREMENT id space, so the named
		// class flipped between an amend and a full rebuild. (idx.byID already
		// holds the incumbent — it was populated in its own iteration.)
		if classLabels[m.Label] {
			cur, seen := idx.classByName[m.Name]
			if !seen {
				idx.classByName[m.Name] = m.ID
			} else if pm, ok := idx.byID[cur]; ok &&
				(m.FilePath < pm.FilePath ||
					(m.FilePath == pm.FilePath &&
						(m.Line < pm.Line || (m.Line == pm.Line && m.ID < pm.ID)))) {
				idx.classByName[m.Name] = m.ID
			}
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

	// stableIDs (optional enrichment for the READS/WRITES site payload): a node's
	// published stable identity lives in nodes.stable_id when the producer stamps
	// it, else in the resolution_symbols sidecar keyed by native_id = nodes.id
	// (the readStableIDs fallback join). Both probes are content-gated — an old
	// graph.db missing either surface simply yields no scope_stable_id key.
	if promoteColumnExists(tx, "nodes", "stable_id") {
		sidSQL := `SELECT id, COALESCE(stable_id, '') FROM nodes`
		if promoteTableExists(tx, "resolution_symbols") {
			sidSQL = `SELECT n.id, COALESCE(NULLIF(n.stable_id, ''), rs.stable_id, '')
			          FROM nodes n
			          LEFT JOIN resolution_symbols rs
			                 ON CAST(rs.native_id AS INTEGER) = n.id`
		}
		if sidRows, err := tx.Query(sidSQL); err == nil {
			for sidRows.Next() {
				var nid int64
				var sid string
				if err := sidRows.Scan(&nid, &sid); err != nil {
					continue
				}
				if sid != "" {
					idx.stableIDs[nid] = sid
				}
			}
			sidRows.Close()
		}
	}

	return idx, nil
}

// promoteTableExists / promoteColumnExists probe sqlite_master /
// pragma_table_info so optional enrichment surfaces degrade to absence on an
// old graph.db instead of erroring the whole pass (correct-or-quiet).
func promoteTableExists(tx *sql.Tx, table string) bool {
	var n int
	if err := tx.QueryRow(
		`SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?`, table).Scan(&n); err != nil {
		return false
	}
	return n > 0
}

func promoteColumnExists(tx *sql.Tx, table, column string) bool {
	var n int
	if err := tx.QueryRow(
		`SELECT count(*) FROM pragma_table_info(?) WHERE name=?`, table, column).Scan(&n); err != nil {
		return false
	}
	return n > 0
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
			tgt = idx.fnlAnyFile(partnerName, partnerLine)
		}
		if tgt == 0 {
			return // partner unresolved -> stays a property
		}
		add(nodeID, tgt, "CO_SERIALIZES", "promote_serde", 1.0, 1, "serde_pair",
			partnerName, src.FilePath, line, true /*undirected*/)
	})
}

// fnlAnyFile resolves an exact (name, line) pair in ANY file — the content-
// smallest (file_path, id) match. idx.fnl is a Go map: ranging it is run-order-
// dependent (the previous `for k, id := range idx.fnl { ...; break }` picked a
// random same-named match), and the ids it stores are AUTOINCREMENT values that
// renumber when a batch amend re-inserts the edited file's nodes at the top of
// the id space. (file_path, id) is the content key — identical under an amend
// and a full rebuild, deterministic across runs. fnl is keyed by (file, name,
// line), so two surviving matches always differ in file.
func (idx *promoteIndexes) fnlAnyFile(name string, line int) int64 {
	best := int64(0)
	bestFile := ""
	for k, id := range idx.fnl {
		if k.name != name || k.line != line {
			continue
		}
		if best == 0 || k.file < bestFile || (k.file == bestFile && id < best) {
			best, bestFile = id, k.file
		}
	}
	return best
}

// ---------------------------------------------------------------------------
// READS/WRITES access-site substrate (statement-level identity for a
// def-site->use-site join).
//
// The edge itself is SYMBOL-level (method -> owning class) and its `metadata`
// column MUST stay the bare field name — contract_map.py matches it byte-exact
// (`e.metadata = ?`) and promote_test's assertTier does too — so the
// statement-level payload rides in the additive `access_sites` column instead
// (JSON object, the api_edges.go convention):
//
//   {"v":1,"field":"count","access":"read","line":6,
//    "scope_node_id":14,"scope_name":"validate",
//    "scope_stable_id":"<node stable id, omitted when none is published>",
//    "sites":[{"field":"count","line":6},{"field":"size","line":7}]}
//
// Edge dedup (edgeKey = source,target,type) collapses EVERY field_read /
// side_effect row of a (method,class) pair into ONE edge — including DIFFERENT
// fields of the same class and, for writes, repeated writes of one field. The
// surviving row is the first in forEachProperty's content order
// (node_id, value, line, id); the accumulator mirrors that first-writer-wins
// pick for the payload's top-level field/line and records EVERY row as a site,
// so the deduped edge still carries its full statement-level footprint.
// `sites` is sorted (line, field) and exact (field,line) duplicates collapse —
// deterministic across runs regardless of parse-worker insertion order.
//
// `column` is genuinely ABSENT at this layer: the parser records only
// node.StartPoint().Row into PropertyRef.Line (the `properties` table has no
// column field), so nothing per-site can be reported but the access line —
// never fabricated.
// ---------------------------------------------------------------------------

// accessSiteEntry is one captured field access: the field touched, the
// statement line it happens on, and the receiver chain it was read/written
// through (``self``/``this``/named receiver — empty only when the producer
// could not name one).
type accessSiteEntry struct {
	Field    string `json:"field"`
	Line     int    `json:"line"`
	Receiver string `json:"receiver,omitempty"`
}

// edgeAccessMeta is the JSON payload written to edges.access_sites on a
// promoted READS/WRITES edge.
type edgeAccessMeta struct {
	V             int               `json:"v"`             // payload schema marker
	Field         string            `json:"field"`         // == edges.metadata (first-writer-wins field)
	Access        string            `json:"access"`        // "read" (READS) / "write" (WRITES)
	Line          int               `json:"line"`          // == edges.source_line (primary access line)
	ScopeNodeID   int64             `json:"scope_node_id"` // == edges.source_id (enclosing method)
	ScopeName     string            `json:"scope_name"`    // enclosing method name
	ScopeStableID string            `json:"scope_stable_id,omitempty"`
	Receiver      string            `json:"receiver,omitempty"` // primary site's receiver chain
	Sites         []accessSiteEntry `json:"sites"`
}

// accessSiteAccum accumulates the per-row access sites that edge dedup
// collapses into one promoted edge. add() must be called AFTER the addEdge
// call it shadows, once per candidate row.
type accessSiteAccum struct {
	byKey map[edgeKey]*edgeAccessMeta
	seen  map[edgeKey]map[accessSiteEntry]bool
}

func newAccessSiteAccum() *accessSiteAccum {
	return &accessSiteAccum{
		byKey: make(map[edgeKey]*edgeAccessMeta),
		seen:  make(map[edgeKey]map[accessSiteEntry]bool),
	}
}

// add records one access row for key. The FIRST row in stream order becomes the
// primary (field/line/scope) — the same row add()'s first-writer-wins dedup
// mints the edge from, since forEachProperty streams a fixed content order and
// add() marks the key seen on that first call.
func (a *accessSiteAccum) add(key edgeKey, field, access, receiver string, line int, src promoteNodeMeta) {
	m, ok := a.byKey[key]
	if !ok {
		m = &edgeAccessMeta{
			V:           2,
			Field:       field,
			Access:      access,
			Line:        line,
			ScopeNodeID: src.ID,
			ScopeName:   src.Name,
			Receiver:    receiver,
		}
		a.byKey[key] = m
	}
	e := accessSiteEntry{Field: field, Line: line, Receiver: receiver}
	if a.seen[key] == nil {
		a.seen[key] = make(map[accessSiteEntry]bool)
	}
	if !a.seen[key][e] {
		a.seen[key][e] = true
		m.Sites = append(m.Sites, e)
	}
}

// marshal renders each accumulated edge's payload as a deterministic JSON
// object (struct field order is fixed; sites are sorted by (line, field);
// scope_stable_id is stamped from idx.stableIDs when the enclosing method has a
// published stable identity, else omitted).
func (a *accessSiteAccum) marshal(idx *promoteIndexes) map[edgeKey]string {
	out := make(map[edgeKey]string, len(a.byKey))
	for key, m := range a.byKey {
		if sid := idx.stableIDs[m.ScopeNodeID]; sid != "" {
			m.ScopeStableID = sid
		}
		sort.Slice(m.Sites, func(i, j int) bool {
			if m.Sites[i].Line != m.Sites[j].Line {
				return m.Sites[i].Line < m.Sites[j].Line
			}
			return m.Sites[i].Field < m.Sites[j].Field
		})
		if b, err := json.Marshal(m); err == nil {
			out[key] = string(b)
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// Class 2 — READS  (field_read: Method reader -> owning Class)
// ---------------------------------------------------------------------------

// promoteFieldReads mints READS edges exactly as before AND returns the
// per-edge access-site payload each minted edge should carry (map keyed by the
// same edgeKey the addEdge dedup uses). The edge's metadata stays the bare
// field name; the JSON rides edges.access_sites.
func promoteFieldReads(db *store.DB, idx *promoteIndexes, add addEdgeFunc) (map[edgeKey]string, error) {
	sites := newAccessSiteAccum()
	err := forEachProperty(db, "field_read", func(nodeID int64, value string, line int) {
		m := fieldReadRe.FindStringSubmatch(value)
		if m == nil {
			return
		}
		receiver, field := m[1], m[2]
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
		if nodeID != 0 && cls.ID != 0 && nodeID != cls.ID { // mirror add()'s non-invention guard
			sites.add(edgeKey{sourceID: nodeID, targetID: cls.ID, typ: "READS"},
				field, "read", receiver, line, src)
		}
	})
	return sites.marshal(idx), err
}

// ---------------------------------------------------------------------------
// Class 3 — WRITES  (side_effect field write: Method writer -> owning Class)
// ---------------------------------------------------------------------------

// promoteWrites is the WRITES twin of promoteFieldReads: same edge semantics,
// plus the access-site payload map for the minted edges. Unlike field_read
// (which the parser dedups to one property per (method, recv.field)), EVERY
// `recv.field = ...` assignment mints its own side_effect property, so a
// method's repeated writes of one field reach here as distinct rows — they
// collapse into ONE edge via dedup, and `sites` keeps every write's line.
func promoteWrites(db *store.DB, idx *promoteIndexes, add addEdgeFunc) (map[edgeKey]string, error) {
	sites := newAccessSiteAccum()
	err := forEachProperty(db, "side_effect", func(nodeID int64, value string, line int) {
		m := fieldWriteRe.FindStringSubmatch(value)
		if m == nil {
			return // side_effect value with no resolvable field/target -> STAY property
		}
		receiver, field := m[1], m[2]
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
		if nodeID != 0 && cls.ID != 0 && nodeID != cls.ID { // mirror add()'s non-invention guard
			sites.add(edgeKey{sourceID: nodeID, targetID: cls.ID, typ: "WRITES"},
				field, "write", receiver, line, src)
		}
	})
	return sites.marshal(idx), err
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
			add(nodeID, tgt, "RAISES", "promote_raises", 0.9, cc, "exception",
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
//  1. RECEIVER TYPE RESOLVES to a concrete class node — `self`/`this`/`super` (the
//     Python/JS/Rust keyword receivers) AND a Go method's RECEIVER VARIABLE
//     (`func (r *T) Run()` calling `r.open()` → token `r`) resolve to the caller
//     method's enclosing class (src.ParentID, which linkGoReceiverMethods parented to
//     the struct); a `self.<field>` or bare-field receiver resolves through the
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
// methods the sequence is calling. `self`/`this`/`super` (and a Go method's own
// receiver variable, `func (r *T) M()` → token `r`) → the caller method's enclosing
// class (src.ParentID, which must be a Class). `self.<field>` or a bare `<field>`
// known on the enclosing class → the field's declared class (via the field-type
// index → classByName). Returns ok=false when no class can be proven.
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
	// self.<field> / this.<field> -> resolve the field on the enclosing class.
	field := ""
	switch {
	case r == "self" || r == "this" || r == "super":
		// Receiver IS the enclosing object → its class is the caller's parent class.
		return idx.enclosingClass(src)
	case strings.HasPrefix(r, "self."):
		field = r[len("self."):]
	case strings.HasPrefix(r, "this."):
		field = r[len("this."):]
	case strings.HasPrefix(r, "super."):
		field = r[len("super."):]
	default:
		// Bare token: only treat it as a receiver if it is a KNOWN field of the
		// enclosing class (else it is a local var of unknown type → abstain).
		field = r
	}
	if field == "" || strings.ContainsAny(field, ". ") {
		return 0, false // chained/compound receiver — too ambiguous to type-prove
	}
	enclosingID, ok := idx.enclosingClass(src)
	if !ok || enclosingID == 0 {
		return 0, false
	}
	typeName, ok := idx.fieldTypes[enclosingID][field]
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
