package main

// Derived analysis layers: the publication-path call site for internal/cochange,
// internal/community and internal/process.
//
// # WHY THIS FILE EXISTS
//
// All three packages landed complete, tested and unreferenced. A package with
// no call site is indistinguishable from a package that was never written: the
// cochanges, communities, community_members, processes and process_steps
// tables were absent or empty in every graph the producer published, and the
// downstream features that read them had nothing to fire on. This file is the
// one place that runs them.
//
// # WHY IT IS A SEPARATE FILE
//
// main.go is a protected path guarded by the fixture-first pre-push hook and is
// edited concurrently by several other work streams. Everything here is
// therefore additive and self-contained; main.go carries a single call, a
// single metadata merge, and the gate.
//
// # WHY A FAILURE IS NEVER SILENT AND NEVER FATAL
//
// These layers are an analysis sidecar over a graph that is already published
// and already correct. A co-change walk that cannot run because the checkout is
// a depth-1 clone must not discard the index -- but it must also not leave a
// table that looks merely uninteresting. Every outcome, including every
// failure, is a NAMED state in project_meta. "The analysis found nothing" and
// "the analysis never ran" are different claims and a reader can always tell
// them apart.
//
// The one way to turn an abstention into a failure is to ask for it:
// GT_REQUIRE_DERIVED=1 fails the build closed on any state but ok, in the style
// of GT_REQUIRE_PARSE_RATE and GT_REQUIRE_FTS5.

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/harneet2512/groundtruth/gt-index/internal/cochange"
	"github.com/harneet2512/groundtruth/gt-index/internal/community"
	"github.com/harneet2512/groundtruth/gt-index/internal/process"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// Environment switches, in the fail-closed style main.go already uses for
// GT_REQUIRE_PARSE_RATE, GT_REQUIRE_FTS5 and GT_HIERARCHY_CLOSED.
const (
	// EnvDerivedLayers turns the whole stage off. It exists so the cost of the
	// derived layers can be measured against a build without them, and so an
	// operator indexing a checkout with no history can decline to pay for a
	// walk that will abstain. Unset means on.
	EnvDerivedLayers = "GT_DERIVED_LAYERS"
	// EnvRequireDerived promotes every non-ok layer state to a fatal error.
	// A shallow clone is a legitimate abstention by default; under this switch
	// it is a failure, which is the entire point of asking for the switch.
	EnvRequireDerived = "GT_REQUIRE_DERIVED"
)

// Layer names, used both as the degraded-state labels and as the project_meta
// key stems.
const (
	layerCochange  = "cochange"
	layerCommunity = "community"
	layerProcess   = "process"
)

// States this file assigns. A layer's state is otherwise the package's own
// Reason constant verbatim, so a reader looks the meaning up in the package
// that produced it rather than in a translation table here.
const (
	// StateOK is the only state GT_REQUIRE_DERIVED accepts.
	StateOK = "ok"
	// StateDegraded is the whole-stage state when at least one layer is not ok.
	StateDegraded = "degraded"
	// StateDisabledByOperator is the whole-stage and per-layer state when
	// GT_DERIVED_LAYERS turned the stage off. It is recorded rather than
	// omitted so a graph built without the layers proves it was built without
	// them.
	StateDisabledByOperator = "disabled_by_operator"

	stateTransactionFailed = "transaction_failed"
	stateExtractFailed     = "extract_failed"
	stateBuildFailed       = "build_failed"
	statePersistFailed     = "persist_failed"
	stateDeriveFailed      = "derive_failed"
)

// project_meta keys. They are named here rather than formatted at the call site
// so a reader can find every key this stage can write by reading one block.
const (
	metaLayersState    = "derived_layers_state"
	metaLayersDegraded = "derived_layers_degraded"

	metaCochangeState          = "derived_cochange_state"
	metaCochangePairs          = "derived_cochange_pairs"
	metaCochangeCommitsScanned = "derived_cochange_commits_scanned"
	metaCochangeCommitsSkipped = "derived_cochange_commits_skipped"
	metaCochangeShallow        = "derived_cochange_shallow"
	metaCochangeWindowStart    = "derived_cochange_window_start"
	metaCochangeWindowEnd      = "derived_cochange_window_end"

	metaCommunityState             = "derived_community_state"
	metaCommunityCount             = "derived_community_count"
	metaCommunityMembers           = "derived_community_members"
	metaCommunityCohesion          = "derived_community_cohesion"
	metaCommunityCertifiedCallRows = "derived_community_certified_call_rows"
	metaCommunityExcludedCallRows  = "derived_community_excluded_call_rows"
	metaCommunityHoldoutCommits    = "derived_community_holdout_commits"
	// The community layer walks a DEEPER window than co-change (fit +
	// holdout) and mixes in the certified call graph, so its reuse proof
	// needs both bounds of its own window and a digest of the call pairs —
	// the co-change four-tuple alone does not pin either input.
	metaCommunityWindowStart = "derived_community_window_start"
	metaCommunityWindowEnd   = "derived_community_window_end"
	metaCommunityCallDigest  = "derived_community_call_pairs_sha256"

	// metaCouplingReused names which coupling layers a batch amend carried
	// from its parent instead of recomputing: "cochange", "community", both
	// comma-joined, or "" when neither was reused.
	metaCouplingReused = "derived_coupling_reused"

	metaProcessState                = "derived_process_state"
	metaProcessCount                = "derived_process_count"
	metaProcessSteps                = "derived_process_steps"
	metaProcessAssertionsScanned    = "derived_process_assertions_scanned"
	metaProcessAssertionsWithTarget = "derived_process_assertions_with_target"
	metaProcessTargetsWithoutPath   = "derived_process_targets_without_path"
	metaProcessTruncated            = "derived_process_truncated"
)

// DerivedOptions is the resolved operator configuration for the stage.
type DerivedOptions struct {
	// Enabled runs the layers. Default true.
	Enabled bool
	// Required makes any layer state other than ok fatal to the build.
	Required bool
}

// resolveDerivedOptions reads the switches. A value it does not recognise is an
// error rather than a default, because silently running a stage an operator
// tried to turn off -- or silently skipping one they tried to require -- is the
// failure mode both switches exist to prevent.
func resolveDerivedOptions(lookup func(string) (string, bool)) (DerivedOptions, error) {
	opts := DerivedOptions{Enabled: true}
	if raw, ok := lookup(EnvDerivedLayers); ok {
		switch strings.ToLower(strings.TrimSpace(raw)) {
		case "", "1", "on", "true":
			opts.Enabled = true
		case "0", "off", "false":
			opts.Enabled = false
		default:
			return opts, fmt.Errorf("%s=%q is not a valid setting: use on or off", EnvDerivedLayers, raw)
		}
	}
	if raw, ok := lookup(EnvRequireDerived); ok {
		switch strings.ToLower(strings.TrimSpace(raw)) {
		case "", "0", "off", "false":
			opts.Required = false
		case "1", "on", "true":
			opts.Required = true
		default:
			return opts, fmt.Errorf("%s=%q is not a valid setting: use 1 or 0", EnvRequireDerived, raw)
		}
	}
	if opts.Required && !opts.Enabled {
		return opts, fmt.Errorf("%s=1 contradicts %s=off: the derived layers cannot be both required and disabled",
			EnvRequireDerived, EnvDerivedLayers)
	}
	return opts, nil
}

// DerivedOutcome is what one run of the stage produced: the rows written per
// table, the named state of every layer, and the receipt fields those states
// are published as.
type DerivedOutcome struct {
	Metadata map[string]string
	// Degraded names every layer whose state is not ok, in layer order.
	Degraded []string
	// Elapsed is the wall time the stage added. It is reported to stderr and
	// deliberately NOT written to project_meta: RC-17/F-004 removed wall-clock
	// fields from the receipt because they break byte-equality between two
	// builds of the same commit.
	Elapsed time.Duration

	CochangePairs    int
	Communities      int
	CommunityMembers int
	Processes        int
	ProcessSteps     int
}

// state records one layer's outcome and adds it to Degraded when it is not ok.
func (o *DerivedOutcome) state(layer, key, value string) {
	o.Metadata[key] = value
	if value != StateOK {
		o.Degraded = append(o.Degraded, layer+"="+value)
	}
}

func (o *DerivedOutcome) put(key string, value interface{}) {
	o.Metadata[key] = fmt.Sprintf("%v", value)
}

// Err reports the stage as a build failure when the operator required it and a
// layer did not deliver. It returns nil in every other case, including every
// degraded state, because a degraded analysis sidecar is not a reason to
// discard a correct graph.
func (o DerivedOutcome) Err(opts DerivedOptions) error {
	if !opts.Required || len(o.Degraded) == 0 {
		return nil
	}
	return fmt.Errorf("%s=1 but the derived layers are degraded: %s",
		EnvRequireDerived, strings.Join(o.Degraded, " "))
}

// Summary is the one stderr line the stage prints, in the shape the other
// passes already print.
func (o DerivedOutcome) Summary() string {
	return fmt.Sprintf("%d co-change pairs, %d communities (%d members), %d processes (%d steps) in %s [%s]",
		o.CochangePairs, o.Communities, o.CommunityMembers, o.Processes, o.ProcessSteps,
		o.Elapsed.Round(time.Millisecond), o.Metadata[metaLayersState])
}

// runDerivedLayers runs the three derived analysis layers over an already
// published graph and returns their outcome. It never returns an error: every
// failure is a named state in the returned metadata, and the caller decides
// whether the operator asked for that to be fatal.
//
// Order is the dependency order. Co-change reads only git history. Communities
// cluster certified CALLS edges together with co-change coupling, so they need
// the resolution graph attached and the history readable. Processes walk
// certified CALLS edges from assertion targets, so they need the resolution
// graph and the assertions table.
//
// Co-change and communities share one transaction so that a graph which commits
// cannot commit half a coupling analysis. Processes own their tables outright
// and publish in their own transaction, which is the seam item 9 splits.
func runDerivedLayers(ctx context.Context, db *store.DB, dbPath, repoPath string, opts DerivedOptions, reuse *couplingReuse) DerivedOutcome {
	out := DerivedOutcome{Metadata: make(map[string]string, 24)}
	if !opts.Enabled {
		disableDerivedOutcome(&out)
		return out
	}
	started := time.Now()
	runCouplingLayers(ctx, db, repoPath, &out, reuse)
	runProcessLayer(ctx, dbPath, &out)
	out.Elapsed = time.Since(started)

	sort.Strings(out.Degraded)
	out.Metadata[metaLayersDegraded] = strings.Join(out.Degraded, ",")
	if len(out.Degraded) == 0 {
		out.Metadata[metaLayersState] = StateOK
	} else {
		out.Metadata[metaLayersState] = StateDegraded
	}
	return out
}

// disableDerivedOutcome writes the full key set with the disabled state rather
// than writing nothing. A graph built with the layers off must still answer
// "why is this table empty" from its own receipt.
func disableDerivedOutcome(out *DerivedOutcome) {
	out.Metadata[metaLayersState] = StateDisabledByOperator
	out.Metadata[metaLayersDegraded] = ""
	for _, key := range []string{metaCochangeState, metaCommunityState, metaProcessState} {
		out.Metadata[key] = StateDisabledByOperator
	}
	for _, key := range []string{
		metaCochangePairs, metaCochangeCommitsScanned, metaCochangeCommitsSkipped, metaCochangeShallow,
		metaCommunityCount, metaCommunityMembers, metaCommunityCertifiedCallRows,
		metaCommunityExcludedCallRows, metaCommunityHoldoutCommits,
		metaProcessCount, metaProcessSteps, metaProcessAssertionsScanned,
		metaProcessAssertionsWithTarget, metaProcessTargetsWithoutPath, metaProcessTruncated,
	} {
		out.Metadata[key] = "0"
	}
	out.Metadata[metaCochangeWindowStart] = ""
	out.Metadata[metaCochangeWindowEnd] = ""
	out.Metadata[metaCommunityCohesion] = "absent:" + StateDisabledByOperator
	out.Metadata[metaCommunityWindowStart] = ""
	out.Metadata[metaCommunityWindowEnd] = ""
	out.Metadata[metaCommunityCallDigest] = ""
	out.Metadata[metaCouplingReused] = ""
}

// The project_meta keys a reused coupling outcome re-emits verbatim: the
// copied graph's receipt must describe the rows it carries exactly the way the
// parent's did, because nothing about them changed.
var (
	couplingCochangeMetaKeys = []string{
		metaCochangeState, metaCochangePairs, metaCochangeCommitsScanned,
		metaCochangeCommitsSkipped, metaCochangeShallow,
		metaCochangeWindowStart, metaCochangeWindowEnd,
	}
	couplingCommunityMetaKeys = []string{
		metaCommunityState, metaCommunityCount, metaCommunityMembers, metaCommunityCohesion,
		metaCommunityCertifiedCallRows, metaCommunityExcludedCallRows, metaCommunityHoldoutCommits,
		metaCommunityWindowStart, metaCommunityWindowEnd, metaCommunityCallDigest,
	}
)

// couplingWindow is the amend's own coupling reuse key: the revision and the
// commit boundaries of the two history windows the coupling layers walk,
// measured WITHOUT the name-only enumeration those walks perform. One rev-list
// of bare commit ids bounds both windows — the expensive part of the walk is
// the per-commit tree diff, not the commit listing.
type couplingWindow struct {
	head    string
	shallow bool
	// windowStart/windowEnd bound the co-change window (DefaultMaxCommits).
	windowStart string
	windowEnd   string
	// communityWindowStart/communityWindowEnd bound the community layer's
	// deeper fit+holdout window (DefaultMaxCommits + DefaultHoldoutCommits).
	communityWindowStart string
	communityWindowEnd   string
}

// couplingReuse carries the parent's recorded coupling outcome — the receipt
// values verbatim, the row counts for the summary line, and whether the
// community layer's extra inputs matched — from the copied parent's
// project_meta into the amend's publication path.
type couplingReuse struct {
	meta map[string]string
	// pairs/communities/members are the parent's recorded row counts, kept for
	// DerivedOutcome's summary line rather than re-derived from the tables.
	pairs       int
	communities int
	members     int
	// communityReusable records that the parent's community outcome is
	// committable (a real state, not a failed/abstained write) and that the
	// community layer's deeper window bounds match. The remaining half of the
	// proof — the certified call-pair digest — can only be checked once the
	// amended edges exist, inside the publication transaction.
	communityReusable bool
}

// recordCochange re-emits the parent's co-change receipt verbatim. The state
// goes through out.state so a parent-recorded abstention still degrades this
// build's receipt exactly as a fresh abstention would.
func (r *couplingReuse) recordCochange(out *DerivedOutcome) {
	for _, key := range couplingCochangeMetaKeys {
		if key == metaCochangeState {
			out.state(layerCochange, key, r.meta[key])
			continue
		}
		out.Metadata[key] = r.meta[key]
	}
	out.CochangePairs = r.pairs
}

// recordCommunity re-emits the parent's community receipt verbatim, under the
// same state-through-out.state rule as recordCochange.
func (r *couplingReuse) recordCommunity(out *DerivedOutcome) {
	for _, key := range couplingCommunityMetaKeys {
		if key == metaCommunityState {
			out.state(layerCommunity, key, r.meta[key])
			continue
		}
		out.Metadata[key] = r.meta[key]
	}
	out.Communities = r.communities
	out.CommunityMembers = r.members
}

// gitRevParse runs `git rev-parse <arg>` in repoPath. repoCommit is the HEAD
// instance of the same call; this exists for the flags it does not expose.
func gitRevParse(repoPath, arg string) (string, bool) {
	out, err := exec.Command("git", "-C", repoPath, "rev-parse", arg).Output()
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(out)), true
}

// currentCouplingWindow measures the checkout's reuse key. `git rev-list
// --no-merges` returns the same commit sequence `git log --no-merges` would —
// both layers parse it with -n caps — so its first and Nth lines are the
// window boundaries each layer would have recorded. No tree diffs are
// computed; this is the boundary probe, not the walk.
func currentCouplingWindow(repoPath string) (couplingWindow, bool) {
	var w couplingWindow
	head := repoCommit(repoPath)
	if head == "" {
		return w, false
	}
	w.head = head
	if s, ok := gitRevParse(repoPath, "--is-shallow-repository"); ok {
		w.shallow = strings.EqualFold(s, "true")
	}
	depth := cochange.DefaultMaxCommits + community.DefaultHoldoutCommits
	out, err := exec.Command("git", "-C", repoPath,
		"rev-list", "--no-merges", "-n", strconv.Itoa(depth), "HEAD").Output()
	if err != nil {
		return couplingWindow{}, false
	}
	var commits []string
	for _, line := range strings.Split(string(out), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			commits = append(commits, line)
		}
	}
	if len(commits) == 0 {
		return couplingWindow{}, false
	}
	w.windowEnd = commits[0]
	cut := cochange.DefaultMaxCommits
	if cut > len(commits) {
		cut = len(commits)
	}
	w.windowStart = commits[cut-1]
	w.communityWindowEnd = commits[0]
	w.communityWindowStart = commits[len(commits)-1]
	return w, true
}

// resolveCouplingReuse decides whether a batch amend can carry the parent's
// coupling tables untouched. It must run BEFORE ReplaceParsedStructure: that
// call clears the staged copy's project_meta, and the copied receipt is the
// only record of the window the parent's coupling was computed over.
//
// The reuse key is the four-tuple the parent recorded — the repository
// revision it was built over (the core receipt's repository_revision, the one
// revision field present on every amendable parent), the shallow flag, and the
// co-change window's oldest and newest commits — compared against the same
// boundary recomputed from the checkout. HEAD alone would not be sufficient:
// two grafted checkouts can share HEAD and still hold different windows.
//
// A nil return means "recompute", the same outcome as today for a parent that
// never recorded the key, a moved HEAD, or a different graft boundary.
func resolveCouplingReuse(db *store.DB, repoPath string) *couplingReuse {
	cur, ok := currentCouplingWindow(repoPath)
	if !ok {
		return nil
	}
	var receipt store.CorePhaseReceipt
	if err := json.Unmarshal([]byte(db.MetaValue(store.CorePhaseReceiptKey)), &receipt); err != nil {
		return nil
	}
	if receipt.RepositoryRevision == "" || receipt.RepositoryRevision == "unversioned" || receipt.RepositoryRevision != cur.head {
		return nil
	}
	parent := make(map[string]string, len(couplingCochangeMetaKeys)+len(couplingCommunityMetaKeys))
	for _, key := range couplingCochangeMetaKeys {
		parent[key] = db.MetaValue(key)
	}
	for _, key := range couplingCommunityMetaKeys {
		parent[key] = db.MetaValue(key)
	}
	// Only a committed co-change outcome is worth carrying: every other state
	// is cheaper to reproduce than to second-guess, and a parent that abstained
	// recorded an empty window this comparison would refuse anyway.
	if parent[metaCochangeState] != StateOK ||
		parent[metaCochangeShallow] != boolMeta(cur.shallow) ||
		parent[metaCochangeWindowStart] == "" || parent[metaCochangeWindowEnd] == "" ||
		parent[metaCochangeWindowStart] != cur.windowStart ||
		parent[metaCochangeWindowEnd] != cur.windowEnd {
		return nil
	}
	reuse := &couplingReuse{meta: parent}
	reuse.pairs, _ = strconv.Atoi(parent[metaCochangePairs])
	reuse.communities, _ = strconv.Atoi(parent[metaCommunityCount])
	reuse.members, _ = strconv.Atoi(parent[metaCommunityMembers])
	// The community partition is NOT a pure function of the history window:
	// it clusters the certified call edges too. Reuse therefore additionally
	// requires the parent's own (deeper) window bounds to match and a recorded
	// call-pair digest to compare inside the publication transaction, once the
	// amended edges exist. A parent without either key simply cannot prove the
	// input is unchanged, so its communities are rebuilt — the same fallback
	// any missing receipt field takes.
	switch parent[metaCommunityState] {
	case community.ReasonOK, community.ReasonNoEdges, community.ReasonNoCommunities:
		reuse.communityReusable = parent[metaCommunityCallDigest] != "" &&
			parent[metaCommunityWindowStart] != "" &&
			parent[metaCommunityWindowStart] == cur.communityWindowStart &&
			parent[metaCommunityWindowEnd] == cur.communityWindowEnd
	}
	return reuse
}

// runCouplingLayers extracts co-change and clusters communities, then publishes
// both in one transaction. On a batch amend whose history window provably
// matches the parent's, either table may instead be carried over untouched —
// reuse decides that per layer, and publishCoupling skips the delete+rewrite
// for whichever the receipt still vouches for.
//
// The analysis outcomes are recorded BEFORE the write is attempted, so a
// failure of the write cannot erase what the analysis actually found; the write
// failure is then recorded over them, which is the honest order because a
// result that did not commit is a result the graph does not have.
func runCouplingLayers(ctx context.Context, db *store.DB, repoPath string, out *DerivedOutcome, reuse *couplingReuse) {
	out.Metadata[metaCouplingReused] = ""
	tx, err := db.BeginTx()
	if err != nil {
		out.state(layerCochange, metaCochangeState, stateTransactionFailed)
		out.state(layerCommunity, metaCommunityState, stateTransactionFailed)
		recordCochange(out, cochange.Result{}, false)
		recordCommunity(out, community.Result{}, false)
		return
	}

	// The co-change table is a pure function of the pinned window, so the
	// matched four-tuple is the whole proof. The community table also clusters
	// the certified call edges, so it is carried only when that second input
	// is provably identical as well — the digest the parent recorded against
	// the amended graph's own edges.
	keepCochange := reuse != nil
	keepCommunity := false
	if keepCochange && reuse.communityReusable {
		digest, derr := community.CertifiedCallGraphDigest(ctx, tx)
		keepCommunity = derr == nil && digest == reuse.meta[metaCommunityCallDigest]
	}
	var reused []string
	if keepCochange {
		reused = append(reused, layerCochange)
	}
	if keepCommunity {
		reused = append(reused, layerCommunity)
	}
	out.Metadata[metaCouplingReused] = strings.Join(reused, ",")

	var coupling cochange.Result
	var communities community.Result
	if keepCochange {
		reuse.recordCochange(out)
	} else {
		var coErr error
		coupling, coErr = cochange.Extract(ctx, repoPath, cochange.Options{})
		recordCochange(out, coupling, coErr == nil)
	}
	if keepCommunity {
		reuse.recordCommunity(out)
	} else {
		var commErr error
		communities, commErr = community.Build(ctx, tx, repoPath, community.Options{})
		recordCommunity(out, communities, commErr == nil)
	}

	if err := publishCoupling(tx, coupling, communities, out, keepCochange, keepCommunity); err != nil {
		_ = tx.Rollback()
		failCoupling(out)
		return
	}
	if err := tx.Commit(); err != nil {
		failCoupling(out)
	}
}

// failCoupling records a write that did not land. The row counts are zeroed
// first: a result that did not commit is a result the graph does not have, and
// a receipt that reports rows the tables do not contain is worse than one that
// reports none.
func failCoupling(out *DerivedOutcome) {
	out.CochangePairs, out.Communities, out.CommunityMembers = 0, 0, 0
	out.Metadata[metaCochangePairs] = "0"
	out.Metadata[metaCommunityCount] = "0"
	out.Metadata[metaCommunityMembers] = "0"
	out.state(layerCochange, metaCochangeState, statePersistFailed)
	out.state(layerCommunity, metaCommunityState, statePersistFailed)
}

// publishCoupling replaces both populations wholesale inside tx. Wholesale
// replacement rather than merge: both are pure functions of the repository at
// this revision, so a row that survives from an earlier state would sit
// indistinguishably beside a current one.
//
// A keep flag skips a population's delete+rewrite entirely: the caller has
// already proved the carried rows are the rows this build would have written,
// so deleting them only to reinsert identical content is churn, not safety.
func publishCoupling(tx *sql.Tx, coupling cochange.Result, communities community.Result, out *DerivedOutcome, keepCochange, keepCommunity bool) error {
	if err := community.EnsureSchema(tx); err != nil {
		return err
	}
	var statements []string
	if !keepCommunity {
		statements = append(statements,
			`DELETE FROM community_members`,
			`DELETE FROM communities`)
	}
	if !keepCochange {
		statements = append(statements, `DELETE FROM cochanges`)
	}
	for _, statement := range statements {
		if _, err := tx.Exec(statement); err != nil {
			return fmt.Errorf("clear derived coupling rows: %w", err)
		}
	}
	if !keepCochange {
		pairs, err := cochange.Persist(tx, coupling)
		if err != nil {
			return err
		}
		out.CochangePairs = pairs
		out.put(metaCochangePairs, pairs)
	}
	if !keepCommunity {
		written, err := community.Persist(tx, communities)
		if err != nil {
			return err
		}
		out.Communities = written
		out.CommunityMembers = 0
		for _, c := range communities.Communities {
			out.CommunityMembers += len(c.Members)
		}
		out.put(metaCommunityCount, written)
		out.put(metaCommunityMembers, out.CommunityMembers)
	}
	return nil
}

// runProcessLayer derives and publishes the test-witnessed processes. It opens
// its own connection, as process.Run documents, because store.DB does not
// expose its *sql.DB and adding an accessor would mean editing the protected
// core-graph store.
func runProcessLayer(ctx context.Context, dbPath string, out *DerivedOutcome) {
	result, err := process.Run(ctx, dbPath, process.Options{})
	if err != nil {
		out.state(layerProcess, metaProcessState, stateDeriveFailed)
		recordProcess(out, result, false)
		return
	}
	state := StateOK
	if result.Reason != "" {
		// A named empty result: assertions absent, none linked, no certified
		// edges, no certified path, or no resolvable stable id. The package
		// names which stage came up empty; that name is the state.
		state = result.Reason
	}
	out.state(layerProcess, metaProcessState, state)
	recordProcess(out, result, true)
	out.Processes = len(result.Processes)
	for _, p := range result.Processes {
		out.ProcessSteps += len(p.Path)
	}
}

func recordCochange(out *DerivedOutcome, result cochange.Result, ran bool) {
	state := stateExtractFailed
	if ran {
		state = result.Reason
	}
	if _, already := out.Metadata[metaCochangeState]; !already {
		out.state(layerCochange, metaCochangeState, state)
	}
	out.put(metaCochangePairs, len(result.Pairs))
	out.put(metaCochangeCommitsScanned, result.CommitsScanned)
	out.put(metaCochangeCommitsSkipped, result.CommitsSkippedTooLarge)
	out.put(metaCochangeShallow, boolMeta(result.Shallow))
	out.Metadata[metaCochangeWindowStart] = result.WindowStart
	out.Metadata[metaCochangeWindowEnd] = result.WindowEnd
}

func recordCommunity(out *DerivedOutcome, result community.Result, ran bool) {
	state := stateBuildFailed
	cohesion := "absent:" + stateBuildFailed
	if ran {
		state = result.Reason
		cohesion = result.CohesionSummary()
	}
	if _, already := out.Metadata[metaCommunityState]; !already {
		out.state(layerCommunity, metaCommunityState, state)
	}
	members := 0
	for _, c := range result.Communities {
		members += len(c.Members)
	}
	out.put(metaCommunityCount, len(result.Communities))
	out.put(metaCommunityMembers, members)
	out.Metadata[metaCommunityCohesion] = cohesion
	out.put(metaCommunityCertifiedCallRows, result.CertifiedCallRows)
	out.put(metaCommunityExcludedCallRows, result.ExcludedCallRows)
	out.put(metaCommunityHoldoutCommits, result.HoldoutCommitsUsed)
	out.Metadata[metaCommunityWindowStart] = result.WindowStart
	out.Metadata[metaCommunityWindowEnd] = result.WindowEnd
	out.Metadata[metaCommunityCallDigest] = result.CallGraphDigest
}

func recordProcess(out *DerivedOutcome, result process.Result, ran bool) {
	steps := 0
	for _, p := range result.Processes {
		steps += len(p.Path)
	}
	out.put(metaProcessCount, len(result.Processes))
	out.put(metaProcessSteps, steps)
	out.put(metaProcessAssertionsScanned, result.AssertionsScanned)
	out.put(metaProcessAssertionsWithTarget, result.AssertionsWithTarget)
	out.put(metaProcessTargetsWithoutPath, result.TargetsWithoutCertifiedPath)
	out.put(metaProcessTruncated, boolMeta(result.Truncated))
	if !ran {
		out.put(metaProcessCount, 0)
		out.put(metaProcessSteps, 0)
	}
}

// boolMeta renders a flag the way project_meta already renders flags: as 1 or
// 0, never as Go's true/false.
func boolMeta(v bool) string {
	if v {
		return "1"
	}
	return "0"
}
