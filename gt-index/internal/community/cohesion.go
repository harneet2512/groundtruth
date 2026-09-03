package community

import "math"

// This file computes the amplification the whole item turns on: a cohesion
// that can be wrong.
//
// # WHAT IS MEASURED
//
// The communities were fitted on commits OLDER than the holdout boundary. The
// newest HoldoutCommits commits were never seen. For every held-out commit
// touching at least two files (and no more than MaxFilesPerCommit -- a
// 500-file reformat is not a prediction test), every file pair in that commit
// is one prediction opportunity, and the partition either predicted it or did
// not.
//
// A pair is scored for a community c when at least ONE endpoint is a member of
// c and BOTH endpoints are clustered. It is a hit when both endpoints are in
// c. So Cohesion answers: when one of my files changed alongside another
// clustered file, how often was that other file also mine?
//
// # THREE DENOMINATOR CHOICES, EACH DELIBERATE
//
//  1. Pairs with an UNCLUSTERED endpoint are excluded from the denominator and
//     counted separately as Result.UncoveredPairs. A README, a lockfile and a
//     CI config carry no certified call edges, so the partition makes no claim
//     about them; charging it for failing to predict them would measure the
//     repository's file composition rather than the partition. The count is
//     published so the exclusion is visible and its size arguable.
//
//  2. A pair is counted ONCE PER COMMIT, not once overall. Each commit is an
//     independent opportunity, and a pair that co-changed in ten held-out
//     commits is ten pieces of evidence, not one.
//
//  3. A pair with both endpoints outside c contributes to neither c's
//     numerator nor its denominator. c is not penalised for a coupling
//     somewhere else in the repository that it never claimed.
//
// # WHAT IS NOT DONE
//
// No number is invented. A community that no held-out commit touched has
// Cohesion = NaN and CohesionReason = no_holdout_pairs, and it persists as
// SQL NULL. A low measured rate is reported as a low measured rate. The whole
// value of the metric is that it is allowed to come out badly.

// nan is a named constructor so that every deliberate absence in this package
// reads as an absence at the call site rather than as arithmetic that went
// wrong.
func nan() float64 { return math.NaN() }

// measureHoldout fills in Cohesion, CohesionInterval and CohesionReason on
// every community, and the partition-wide figures on res. It mutates cs in
// place.
func measureHoldout(cs []Community, hist history, opts Options, res *Result) {
	res.OverallCohesion = nan()
	res.OverallCohesionInterval = Interval{Lo: nan(), Hi: nan()}

	reason := ""
	switch {
	case opts.HoldoutCommits <= 0:
		reason = CohesionHoldoutDisabled
	case hist.Reason != "ok":
		reason = CohesionHistoryUnavailable
	case len(hist.Holdout) == 0:
		reason = CohesionNoHoldoutCommits
	}
	if reason != "" {
		res.CohesionReason = reason
		for i := range cs {
			cs[i].Cohesion = nan()
			cs[i].CohesionInterval = Interval{Lo: nan(), Hi: nan()}
			cs[i].CohesionReason = reason
		}
		return
	}

	// file -> index into cs, or absent when the file is in no community.
	owner := make(map[string]int)
	for i := range cs {
		for _, m := range cs[i].Members {
			owner[m] = i
		}
	}

	inside := make([]int, len(cs))
	total := make([]int, len(cs))

	for _, commit := range hist.Holdout {
		files := commit.Files
		for i := 0; i < len(files); i++ {
			ci, iOK := owner[files[i]]
			for j := i + 1; j < len(files); j++ {
				cj, jOK := owner[files[j]]
				if !iOK || !jOK {
					res.UncoveredPairs++
					continue
				}
				res.CoveredPairs++
				if ci == cj {
					res.InsidePairs++
					inside[ci]++
					total[ci]++
					continue
				}
				total[ci]++
				total[cj]++
			}
		}
	}

	for i := range cs {
		if total[i] == 0 {
			cs[i].Cohesion = nan()
			cs[i].CohesionInterval = Interval{Lo: nan(), Hi: nan()}
			cs[i].CohesionReason = CohesionNoHoldoutPairs
			continue
		}
		lo, hi := wilson(inside[i], total[i])
		cs[i].Cohesion = float64(inside[i]) / float64(total[i])
		cs[i].CohesionInterval = Interval{Lo: lo, Hi: hi, N: total[i]}
		cs[i].CohesionReason = CohesionMeasured
	}

	if res.CoveredPairs == 0 {
		res.CohesionReason = CohesionNoHoldoutPairs
		return
	}
	lo, hi := wilson(res.InsidePairs, res.CoveredPairs)
	res.OverallCohesion = float64(res.InsidePairs) / float64(res.CoveredPairs)
	res.OverallCohesionInterval = Interval{Lo: lo, Hi: hi, N: res.CoveredPairs}
	res.CohesionReason = CohesionMeasured
}
