package main

import (
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/cochange"
	"github.com/harneet2512/groundtruth/gt-index/internal/community"
	"github.com/harneet2512/groundtruth/gt-index/internal/process"
)

func lookupFrom(pairs map[string]string) func(string) (string, bool) {
	return func(key string) (string, bool) {
		v, ok := pairs[key]
		return v, ok
	}
}

func TestDerivedOptionsDefaultToRunAndRecord(t *testing.T) {
	opts, err := resolveDerivedOptions(lookupFrom(nil))
	if err != nil {
		t.Fatal(err)
	}
	if !opts.Enabled {
		t.Error("derived layers are off by default; the default must be run-and-record")
	}
	if opts.Required {
		t.Error("derived layers are required by default; requiring them must be opt-in")
	}
}

func TestDerivedOptionsSwitches(t *testing.T) {
	cases := []struct {
		env              map[string]string
		enabled, require bool
	}{
		{map[string]string{EnvDerivedLayers: "off"}, false, false},
		{map[string]string{EnvDerivedLayers: "0"}, false, false},
		{map[string]string{EnvDerivedLayers: "FALSE"}, false, false},
		{map[string]string{EnvDerivedLayers: "on"}, true, false},
		{map[string]string{EnvRequireDerived: "1"}, true, true},
		{map[string]string{EnvRequireDerived: "0"}, true, false},
		{map[string]string{EnvDerivedLayers: "on", EnvRequireDerived: "true"}, true, true},
	}
	for _, tc := range cases {
		opts, err := resolveDerivedOptions(lookupFrom(tc.env))
		if err != nil {
			t.Fatalf("%v: %v", tc.env, err)
		}
		if opts.Enabled != tc.enabled || opts.Required != tc.require {
			t.Errorf("%v -> enabled=%t required=%t, want enabled=%t required=%t",
				tc.env, opts.Enabled, opts.Required, tc.enabled, tc.require)
		}
	}
}

// A setting nobody can spell must not be silently reinterpreted as its default.
// An operator who typed GT_DERIVED_LAYERS=disabled meant off, and running the
// stage anyway is exactly the class of failure the fail-closed gates exist to
// prevent.
func TestDerivedOptionsFailClosedOnAnUnreadableSetting(t *testing.T) {
	for _, env := range []map[string]string{
		{EnvDerivedLayers: "disabled"},
		{EnvDerivedLayers: "yes"},
		{EnvRequireDerived: "please"},
		{EnvDerivedLayers: "off", EnvRequireDerived: "1"},
	} {
		if _, err := resolveDerivedOptions(lookupFrom(env)); err == nil {
			t.Errorf("%v was accepted; it must fail closed", env)
		}
	}
}

func TestDerivedOutcomeErrIsFatalOnlyWhenRequired(t *testing.T) {
	degraded := DerivedOutcome{
		Metadata: map[string]string{metaLayersState: StateDegraded},
		Degraded: []string{"cochange=" + cochange.ReasonShallowClone},
	}
	if err := degraded.Err(DerivedOptions{Enabled: true}); err != nil {
		t.Errorf("a degraded sidecar aborted a default build: %v", err)
	}
	err := degraded.Err(DerivedOptions{Enabled: true, Required: true})
	if err == nil {
		t.Fatal("GT_REQUIRE_DERIVED=1 accepted a degraded state")
	}
	if !strings.Contains(err.Error(), cochange.ReasonShallowClone) {
		t.Errorf("the failure does not name the degraded state: %v", err)
	}

	healthy := DerivedOutcome{Metadata: map[string]string{metaLayersState: StateOK}}
	if err := healthy.Err(DerivedOptions{Enabled: true, Required: true}); err != nil {
		t.Errorf("GT_REQUIRE_DERIVED=1 rejected an ok stage: %v", err)
	}
}

// A disabled stage writes the whole key set rather than nothing. A graph built
// without the layers must be able to say so from its own receipt; an absent key
// is indistinguishable from an older binary that had no layers at all.
func TestDisabledDerivedOutcomeStillNamesEveryLayer(t *testing.T) {
	out := DerivedOutcome{Metadata: map[string]string{}}
	disableDerivedOutcome(&out)

	required := []string{
		metaLayersState, metaLayersDegraded,
		metaCochangeState, metaCochangePairs, metaCochangeCommitsScanned,
		metaCochangeCommitsSkipped, metaCochangeShallow,
		metaCochangeWindowStart, metaCochangeWindowEnd,
		metaCommunityState, metaCommunityCount, metaCommunityMembers, metaCommunityCohesion,
		metaCommunityCertifiedCallRows, metaCommunityExcludedCallRows, metaCommunityHoldoutCommits,
		metaProcessState, metaProcessCount, metaProcessSteps, metaProcessAssertionsScanned,
		metaProcessAssertionsWithTarget, metaProcessTargetsWithoutPath, metaProcessTruncated,
	}
	for _, key := range required {
		if _, ok := out.Metadata[key]; !ok {
			t.Errorf("a disabled stage omitted %s", key)
		}
	}
	for _, key := range []string{metaLayersState, metaCochangeState, metaCommunityState, metaProcessState} {
		if out.Metadata[key] != StateDisabledByOperator {
			t.Errorf("%s = %q, want %q", key, out.Metadata[key], StateDisabledByOperator)
		}
	}
	if len(out.Degraded) != 0 {
		t.Errorf("a stage the operator turned off is reported as degraded: %v", out.Degraded)
	}
	if got := out.Metadata[metaCommunityCohesion]; got != "absent:"+StateDisabledByOperator {
		t.Errorf("cohesion = %q, want an absent:<reason> rendering, never a number", got)
	}
}

// An abstention carries the package's own Reason verbatim, so the receipt says
// which of the named boundaries was hit rather than a generic failure.
func TestAbstentionsAreRecordedUnderTheirOwnNames(t *testing.T) {
	out := DerivedOutcome{Metadata: map[string]string{}}
	recordCochange(&out, cochange.Result{Reason: cochange.ReasonShallowClone, Shallow: true}, true)
	recordCommunity(&out, community.Result{Reason: community.ReasonNoEdges}, true)
	recordProcess(&out, process.Result{Reason: process.ReasonNoAssertions, AssertionsScanned: 0}, true)

	if out.Metadata[metaCochangeState] != cochange.ReasonShallowClone {
		t.Errorf("cochange state = %q, want %q", out.Metadata[metaCochangeState], cochange.ReasonShallowClone)
	}
	if out.Metadata[metaCochangeShallow] != "1" {
		t.Errorf("shallow flag = %q, want 1", out.Metadata[metaCochangeShallow])
	}
	if out.Metadata[metaCommunityState] != community.ReasonNoEdges {
		t.Errorf("community state = %q, want %q", out.Metadata[metaCommunityState], community.ReasonNoEdges)
	}
	if len(out.Degraded) != 2 {
		t.Errorf("degraded layers = %v, want the two abstaining layers", out.Degraded)
	}
	for _, name := range out.Degraded {
		if !strings.Contains(name, "=") {
			t.Errorf("degraded entry %q does not name a state", name)
		}
	}
}

// A write that did not commit must not be reported as rows the tables contain.
func TestAFailedCouplingWriteReportsNoRows(t *testing.T) {
	out := DerivedOutcome{
		Metadata:      map[string]string{metaCochangePairs: "17", metaCommunityCount: "4", metaCommunityMembers: "40"},
		CochangePairs: 17, Communities: 4, CommunityMembers: 40,
	}
	failCoupling(&out)
	for key, want := range map[string]string{metaCochangePairs: "0", metaCommunityCount: "0", metaCommunityMembers: "0"} {
		if out.Metadata[key] != want {
			t.Errorf("%s = %q after a rolled-back write, want %q", key, out.Metadata[key], want)
		}
	}
	if out.CochangePairs != 0 || out.Communities != 0 || out.CommunityMembers != 0 {
		t.Error("row counts survived a rolled-back write")
	}
	if len(out.Degraded) != 2 {
		t.Errorf("degraded layers = %v, want both coupling layers", out.Degraded)
	}
}
