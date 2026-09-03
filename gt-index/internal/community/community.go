// Package community groups a repository's FILES into communities by clustering
// a trust-weighted multigraph of certified call edges and evolutionary
// co-change, and stores a cohesion that can be proved wrong.
//
// # WHAT IS COPIED AND WHAT IS AMPLIFIED
//
// The node shape is copied deliberately, including the discipline worth
// copying: HeuristicLabel is kept ALONGSIDE Label, and EnrichedBy records
// which one a reader is holding. There is no model in this package, so
// EnrichedBy is always EnrichedByHeuristic and Label always starts equal to
// HeuristicLabel. A downstream enricher may overwrite Label and set its own
// EnrichedBy; it may not overwrite HeuristicLabel, so the ungoverned fallback
// is never lost and a reader can always tell a derived label from a generated
// one.
//
// Three things are amplified past that shape:
//
//  1. The graph is TRUST-WEIGHTED. Only CERTIFIED `CALLS` edges contribute
//     structural weight; CANDIDATE, SPECULATIVE and STRUCTURAL edges are
//     excluded by the query itself (see loadCertifiedCallEdges), not filtered
//     later, so a resolver guess cannot merge two communities. Co-change
//     support -- a signal a snapshot-shaped store cannot hold at all -- is
//     added as a second, independently weighted edge population.
//
//  2. The objective is the Constant Potts Model with an explicit resolution
//     parameter, NOT raw modularity. Modularity optimisation has a resolution
//     limit that hides small communities inside large ones; CPM's gamma is a
//     stated density threshold instead of an emergent one. The algorithm is a
//     deterministic Leiden-style local moving / refinement / aggregation loop
//     -- see cluster.go for exactly what was and was not built.
//
//  3. Cohesion is FALSIFIABLE. It is not a property of the partition. The
//     newest HoldoutCommits commits are withheld, the communities are fitted
//     on the older commits only, and Cohesion is the measured rate at which a
//     community's members co-changed with each other rather than with the rest
//     of the clustered repository, over that unseen history, with a Wilson 95%
//     interval and the sample size. When the holdout cannot answer, Cohesion
//     is NaN and CohesionReason says why. A number is never invented to fill
//     the field. The descriptive score is still available, separately, as
//     StructuralCohesion -- it is a different claim and it is labelled as one.
//
// # THE INVARIANT
//
// Community membership NEVER changes an edge or a trust tier. This is enforced
// by API surface, not by convention: the only statement this package makes
// about `edges` is a SELECT (graph.go), and the only statements it makes at
// all are that SELECT, the two CREATE TABLE IF NOT EXISTS for its own tables,
// and INSERTs into those two tables (persist.go). There is no code path here
// that can UPDATE a tier, and TestInvariantEdgesAndTiersUnchanged asserts it
// against a real database.
//
// # BOUNDED BY CONSTRUCTION
//
// Every Options field bounds work, and a zero field means "use the default",
// never "unlimited" -- the same contract internal/cochange uses. The history
// walk is capped at MaxCommits+HoldoutCommits commits and MaxFilesPerCommit
// files per commit; clustering is capped at MaxIterations sweeps; output is
// capped at MaxCommunities and floored at MinMembers; evidence is capped at
// MaxEvidenceEdges per community and truncation is recorded rather than
// hidden.
//
// Zero new go.mod dependencies -- stdlib, the git binary the indexer already
// requires, and internal/cochange for its Pair type and reason vocabulary.
package community

import (
	"context"
	"fmt"
	"math"
	"sort"

	"github.com/harneet2512/groundtruth/gt-index/internal/cochange"
)

// EnrichedByHeuristic is the only value this package ever writes to
// Community.EnrichedBy. It exists as a named constant so that a future
// enricher must choose a different one deliberately, and so that nothing here
// can accidentally be labelled as if a model produced it.
const EnrichedByHeuristic = "heuristic"

// AlgorithmCPMLeidenDeterministic names the clustering actually implemented,
// and is stored on every row. A partition is only reproducible if the reader
// knows which objective and which algorithm produced it.
const AlgorithmCPMLeidenDeterministic = "cpm_leiden_deterministic_v1"

// Defaults. A zero Options field is replaced by the matching default; zero is
// never "unlimited".
const (
	// DefaultWCall weights one CERTIFIED CALLS edge between two files.
	DefaultWCall = 1.0
	// DefaultWCochange weights one unit of co-change support. It equals
	// DefaultWCall so that, at the defaults, "these two files call each other
	// once" and "these two files were edited together once" carry the same
	// evidential weight and neither population can silently dominate.
	DefaultWCochange = 1.0
	// DefaultResolution is the CPM gamma: the edge-weight density a group of
	// files must exceed internally to be worth calling a community. At 0.25
	// with the default weights, two files need a combined call+co-change
	// weight above 0.25 to be worth grouping, and a 10-file community must
	// carry more than 0.25*45 = 11.25 units of internal weight. Lower gamma
	// yields fewer, larger communities; higher gamma yields more, smaller
	// ones. It is a knob, not a constant of nature, which is the whole point
	// of using CPM instead of modularity.
	DefaultResolution = 0.25
	// DefaultMaxCommunities caps the published partition.
	DefaultMaxCommunities = 500
	// DefaultMinMembers is 2: a single file is not a community, it is a file.
	DefaultMinMembers = 2
	// DefaultHoldoutCommits is the number of newest commits withheld from the
	// fit and used to test it, matching the plan's stated default of 50.
	DefaultHoldoutCommits = 50
	// DefaultMaxCommits is the fit window, matching cochange.DefaultMaxCommits
	// so the two signals are derived from comparable histories.
	DefaultMaxCommits = cochange.DefaultMaxCommits
	// DefaultMaxFilesPerCommit is the mass-change cutoff, matching
	// cochange.DefaultMaxFilesPerCommit. It applies to the holdout as well as
	// the fit: a 500-file reformat is not a prediction test, it is noise that
	// would dominate the denominator quadratically.
	DefaultMaxFilesPerCommit = cochange.DefaultMaxFilesPerCommit
	// DefaultMaxIterations bounds the outer Leiden loop. The loop also exits
	// as soon as an aggregation stops shrinking the graph, so this is a
	// backstop against a pathological oscillation, not the usual exit.
	DefaultMaxIterations = 20
	// DefaultMaxEvidenceEdges caps stored evidence per community. Chosen to
	// hold every internal edge of an ordinary community outright; truncation
	// is recorded on the community when it happens.
	DefaultMaxEvidenceEdges = 512
)

// Reasons carried by Result.Reason. Exactly one is always set, including on
// success, so an empty partition is never ambiguous.
const (
	// ReasonOK means the graph was clustered and the communities are the
	// answer. It says nothing about whether cohesion could be measured; that
	// is Result.CohesionReason.
	ReasonOK = "ok"
	// ReasonNoEdges means the weighted graph had no edges at all: no CERTIFIED
	// CALLS edges crossing a file boundary and no co-change support. There is
	// nothing to cluster, and that is an answer about the graph, not a
	// failure.
	ReasonNoEdges = "no_edges"
	// ReasonNoCommunities means the graph had edges but nothing survived
	// MinMembers -- every group the objective found was a single file.
	ReasonNoCommunities = "no_communities"
)

// Reasons carried by Community.CohesionReason and Result.CohesionReason.
const (
	// CohesionMeasured means Cohesion is a real measured rate on unseen
	// commits and the Wilson interval is meaningful.
	CohesionMeasured = "measured"
	// CohesionNoHoldoutCommits means the holdout window contained no commit
	// touching at least two files within the mass-change cap, so nothing could
	// be predicted. Cohesion is NaN.
	CohesionNoHoldoutCommits = "no_holdout_commits"
	// CohesionNoHoldoutPairs means the holdout had qualifying commits but none
	// of their file pairs touched this community's members, so this community
	// was never tested. Cohesion is NaN. This is the honest answer for a
	// community of files nobody edited in the holdout window.
	CohesionNoHoldoutPairs = "no_holdout_pairs"
	// CohesionHoldoutDisabled means HoldoutCommits was set to a value that
	// withheld nothing, so no falsification was attempted. Cohesion is NaN.
	CohesionHoldoutDisabled = "holdout_disabled"
	// CohesionHistoryUnavailable means the repository could not supply history
	// (not a git work tree, git missing, unborn HEAD, or a shallow checkout
	// too short to fill the fit window). Cohesion is NaN.
	CohesionHistoryUnavailable = "history_unavailable"
)

// Interval is a Wilson score interval on a measured proportion. N is the
// denominator that produced it, so a rate of 1.0 from a single observation is
// visibly not the same claim as a rate of 1.0 from four hundred.
type Interval struct {
	Lo float64
	Hi float64
	N  int
}

// Community is one cluster of files. The field set is the copied node shape
// plus the fields that make cohesion falsifiable.
type Community struct {
	// ID is content-addressed over the sorted member list, so the same
	// partition yields the same IDs on every run and across machines.
	ID string
	// Label is what a reader should display. It DEFAULTS to HeuristicLabel and
	// is only ever different if something outside this package overwrote it.
	Label string
	// HeuristicLabel is always set, derived from the members' own paths. It is
	// never overwritten, so the model-free answer survives enrichment.
	HeuristicLabel string
	// Keywords are deterministic path-derived terms, most frequent first.
	Keywords []string
	// Description is a deterministic rendering of the community's own
	// measurements. It is not prose about what the code means.
	Description string
	// EnrichedBy records what produced Label. This package writes only
	// EnrichedByHeuristic.
	EnrichedBy string
	// Cohesion is the MEASURED held-out co-modification prediction rate, in
	// [0,1], or NaN when it could not be measured -- see CohesionReason. It is
	// deliberately NOT a structural score.
	Cohesion float64
	// CohesionInterval is the Wilson 95% interval and sample size behind
	// Cohesion. N is 0 whenever Cohesion is NaN.
	CohesionInterval Interval
	// CohesionReason is always set; see the Cohesion* constants.
	CohesionReason string
	// StructuralCohesion is the descriptive score kept separately and
	// explicitly: internal weight / (internal + external) weight, in [0,1]. It
	// is a property of the partition and predicts nothing.
	StructuralCohesion float64
	// Members are the community's file paths, sorted. Members are files rather
	// than symbol stable_ids because the co-change signal is file-granular:
	// clustering symbols against file-level coupling would fabricate a
	// precision the evidence does not have.
	Members []string
	// EvidenceEdgeIDs are the edges behind this community. A CERTIFIED CALLS
	// edge appears as its decimal `edges.id`; a co-change contribution appears
	// as "cochange:<file_a>|<file_b>", because co-change has no edge row. This
	// is what makes a wrong label traceable to what produced it.
	EvidenceEdgeIDs []string
	// EvidenceTruncated is true when EvidenceEdgeIDs hit MaxEvidenceEdges. The
	// evidence set is then a sample, and says so.
	EvidenceTruncated bool
	// InternalWeight and ExternalWeight are the raw weight sums the structural
	// score was computed from, retained so it can be audited rather than taken
	// on trust.
	InternalWeight float64
	ExternalWeight float64
}

// Options bounds one Build. Normalize replaces every zero field with its
// default; zero never means unlimited.
type Options struct {
	// WCall multiplies the count of CERTIFIED CALLS edges between two files.
	WCall float64
	// WCochange multiplies co-change support between two files.
	WCochange float64
	// Resolution is the CPM gamma. Higher means more, smaller communities.
	Resolution float64
	// MaxCommunities caps how many communities are returned, largest first.
	MaxCommunities int
	// MinMembers drops any community smaller than this.
	MinMembers int
	// HoldoutCommits is how many of the newest commits are withheld from the
	// fit and used to measure Cohesion. Set it negative to disable holdout
	// explicitly and accept that Cohesion will be NaN everywhere; 0 means
	// "use DefaultHoldoutCommits", per the zero-is-default contract.
	HoldoutCommits int
	// MaxCommits is the fit window, measured in commits OLDER than the
	// holdout.
	MaxCommits int
	// MaxFilesPerCommit skips (and counts) any commit touching more files, in
	// both the fit and the holdout.
	MaxFilesPerCommit int
	// MaxIterations bounds the outer clustering loop.
	MaxIterations int
	// MaxEvidenceEdges caps stored evidence per community.
	MaxEvidenceEdges int
}

// Normalize returns the effective options. It is exported so a caller can
// record the bounds actually in force rather than the bounds it asked for.
func (o Options) Normalize() Options {
	if o.WCall <= 0 {
		o.WCall = DefaultWCall
	}
	if o.WCochange <= 0 {
		o.WCochange = DefaultWCochange
	}
	if o.Resolution <= 0 {
		o.Resolution = DefaultResolution
	}
	if o.MaxCommunities <= 0 {
		o.MaxCommunities = DefaultMaxCommunities
	}
	if o.MinMembers <= 0 {
		o.MinMembers = DefaultMinMembers
	}
	if o.HoldoutCommits == 0 {
		o.HoldoutCommits = DefaultHoldoutCommits
	} else if o.HoldoutCommits < 0 {
		o.HoldoutCommits = 0
	}
	if o.MaxCommits <= 0 {
		o.MaxCommits = DefaultMaxCommits
	}
	if o.MaxFilesPerCommit <= 0 {
		o.MaxFilesPerCommit = DefaultMaxFilesPerCommit
	}
	if o.MaxIterations <= 0 {
		o.MaxIterations = DefaultMaxIterations
	}
	if o.MaxEvidenceEdges <= 0 {
		o.MaxEvidenceEdges = DefaultMaxEvidenceEdges
	}
	return o
}

// Result is the complete outcome of one Build, including the boundaries and
// the history split that produced it.
type Result struct {
	// Communities are sorted by descending member count, then by first member
	// path, so the order is deterministic and the largest are first.
	Communities []Community
	// Reason is always set; see the Reason* constants.
	Reason string
	// Options are the EFFECTIVE bounds, after defaults were applied.
	Options Options
	// Algorithm names the clustering that ran.
	Algorithm string

	// Files, CallEdges and CochangeEdges describe the clustered graph: the
	// number of distinct files with at least one edge, the number of distinct
	// file pairs carrying certified call weight, and the number carrying
	// co-change weight.
	Files         int
	CallEdges     int
	CochangeEdges int
	// CertifiedCallRows is the number of CERTIFIED CALLS edge rows that
	// crossed a file boundary and therefore contributed weight.
	CertifiedCallRows int
	// ExcludedCallRows counts CALLS edge rows rejected for their trust tier.
	// It is the proof that the exclusion happened, sized.
	ExcludedCallRows int
	// CommunitiesDropped counts communities the objective found that did not
	// survive MinMembers or MaxCommunities. They are dropped, never merged
	// into a neighbour to make the cap fit.
	CommunitiesDropped int

	// FitCommits and HoldoutCommitsUsed are the commits actually used, after
	// the mass-change cap. FitSkipped and HoldoutSkipped count the ones the
	// cap refused, so a truncated window is visibly truncated.
	FitCommits         int
	HoldoutCommitsUsed int
	FitSkipped         int
	HoldoutSkipped     int
	// HoldoutOldest and HoldoutNewest bound the falsification window.
	HoldoutOldest string
	HoldoutNewest string
	// HistoryReason is the co-change extraction reason for the fit window,
	// reusing internal/cochange's vocabulary.
	HistoryReason string
	// Shallow records that the checkout is grafted, independently of whether
	// that cost anything.
	Shallow bool

	// OverallCohesion is the held-out prediction rate across the whole
	// partition: of the held-out co-changed file pairs whose BOTH endpoints
	// are clustered, the fraction that landed inside one community. Its
	// denominator is stated in OverallCohesionInterval.N.
	OverallCohesion         float64
	OverallCohesionInterval Interval
	// CoveredPairs and InsidePairs are that rate's raw counts.
	CoveredPairs int
	InsidePairs  int
	// UncoveredPairs counts held-out co-changed pairs with at least one
	// endpoint in no community -- files the partition makes no claim about,
	// typically documentation, config and lockfiles that carry no call edges.
	// They are reported rather than folded into the denominator, because
	// dividing by them would measure repository composition, not the
	// partition.
	UncoveredPairs int
	// CohesionReason is always set; see the Cohesion* constants.
	CohesionReason string
}

// Build clusters repoRoot's graph and measures the partition against held-out
// history.
//
// q is any *sql.DB or *sql.Tx; the package issues one SELECT against it and
// nothing else. repoRoot is the checkout the graph was built from, used only
// to read git history.
//
// It returns a non-nil error only when the database read or the history walk
// failed unexpectedly. A repository that legitimately cannot answer -- no
// certified edges, no history -- is a successful call whose Result carries the
// explicit Reason.
func Build(ctx context.Context, q Queryer, repoRoot string, opts Options) (Result, error) {
	effective := opts.Normalize()
	res := Result{Options: effective, Algorithm: AlgorithmCPMLeidenDeterministic}
	res.OverallCohesion = math.NaN()

	calls, stats, err := loadCertifiedCallEdges(ctx, q)
	if err != nil {
		return res, err
	}
	res.CertifiedCallRows = stats.certified
	res.ExcludedCallRows = stats.excluded

	hist, err := loadHistory(ctx, repoRoot, effective)
	if err != nil {
		return res, err
	}
	res.HistoryReason = hist.Reason
	res.Shallow = hist.Shallow
	res.FitCommits = hist.FitCommits
	res.HoldoutCommitsUsed = len(hist.Holdout)
	res.FitSkipped = hist.FitSkipped
	res.HoldoutSkipped = hist.HoldoutSkipped
	res.HoldoutOldest = hist.HoldoutOldest
	res.HoldoutNewest = hist.HoldoutNewest

	g := buildFileGraph(calls, hist.FitPairs, effective)
	res.Files = len(g.files)
	res.CallEdges = g.callPairs
	res.CochangeEdges = g.cochangePairs
	if len(g.files) == 0 || g.edgeCount == 0 {
		res.Reason = ReasonNoEdges
		res.CohesionReason = cohesionReasonFor(hist, effective)
		return res, nil
	}

	partition := cluster(g, effective)

	built, dropped := g.materialize(partition, effective)
	res.CommunitiesDropped = dropped
	if len(built) == 0 {
		res.Reason = ReasonNoCommunities
		res.CohesionReason = cohesionReasonFor(hist, effective)
		return res, nil
	}

	measureHoldout(built, hist, effective, &res)

	for i := range built {
		built[i].Description = describe(built[i])
	}
	sortCommunities(built)
	res.Communities = built
	res.Reason = ReasonOK
	return res, nil
}

// CohesionSummary renders the partition-wide held-out cohesion as one receipt
// field. An absent rate renders as "absent:<reason>", never as a number and
// never as an empty string, so a receipt can always be read without a second
// lookup to find out whether the analysis was measured, unmeasurable, or never
// attempted.
//
// It exists so a caller writing a receipt does not have to re-implement the
// NaN convention -- and get it wrong in the one direction that matters, by
// printing NaN as 0.
func (r Result) CohesionSummary() string {
	if math.IsNaN(r.OverallCohesion) {
		reason := r.CohesionReason
		if reason == "" {
			reason = "unknown"
		}
		return "absent:" + reason
	}
	return fmt.Sprintf("%.4f[%.4f,%.4f]n=%d",
		r.OverallCohesion, r.OverallCohesionInterval.Lo, r.OverallCohesionInterval.Hi,
		r.OverallCohesionInterval.N)
}

// cohesionReasonFor names why no rate could be produced, for the paths that
// exit before measurement runs.
func cohesionReasonFor(hist history, opts Options) string {
	if opts.HoldoutCommits <= 0 {
		return CohesionHoldoutDisabled
	}
	if hist.Reason != cochange.ReasonOK {
		return CohesionHistoryUnavailable
	}
	if len(hist.Holdout) == 0 {
		return CohesionNoHoldoutCommits
	}
	return CohesionNoHoldoutPairs
}

// sortCommunities imposes the published order: largest first, ties broken by
// the first member path, then by ID. Two runs over the same graph therefore
// emit the same list in the same order.
func sortCommunities(cs []Community) {
	sort.SliceStable(cs, func(i, j int) bool {
		if len(cs[i].Members) != len(cs[j].Members) {
			return len(cs[i].Members) > len(cs[j].Members)
		}
		if cs[i].Members[0] != cs[j].Members[0] {
			return cs[i].Members[0] < cs[j].Members[0]
		}
		return cs[i].ID < cs[j].ID
	})
}

// wilson returns the Wilson score interval for successes out of n at 95%
// confidence. The Wilson interval is used rather than the normal
// approximation because the normal interval is degenerate exactly where these
// measurements live: small n, and rates near 0 or 1, where it produces
// impossible bounds outside [0,1] and a zero-width interval at p=0.
//
// n <= 0 yields (NaN, NaN); there is no interval around no observations.
func wilson(successes, n int) (float64, float64) {
	if n <= 0 {
		return math.NaN(), math.NaN()
	}
	const z = 1.959963984540054 // two-sided 95%
	nf := float64(n)
	p := float64(successes) / nf
	denom := 1 + z*z/nf
	centre := (p + z*z/(2*nf)) / denom
	spread := (z / denom) * math.Sqrt(p*(1-p)/nf+z*z/(4*nf*nf))
	lo := centre - spread
	hi := centre + spread
	if lo < 0 {
		lo = 0
	}
	if hi > 1 {
		hi = 1
	}
	return lo, hi
}

// describe renders a community's own measurements. It states what was
// measured and, when cohesion is absent, states that instead of omitting it --
// a description that quietly drops the number reads like a community that
// passed.
func describe(c Community) string {
	if math.IsNaN(c.Cohesion) {
		return fmt.Sprintf(
			"%d files under %s; structural cohesion %.3f; held-out cohesion unmeasured (%s).",
			len(c.Members), c.HeuristicLabel, c.StructuralCohesion, c.CohesionReason)
	}
	return fmt.Sprintf(
		"%d files under %s; structural cohesion %.3f; held-out co-change prediction rate %.3f (95%% CI %.3f-%.3f, n=%d).",
		len(c.Members), c.HeuristicLabel, c.StructuralCohesion,
		c.Cohesion, c.CohesionInterval.Lo, c.CohesionInterval.Hi, c.CohesionInterval.N)
}
