package community

import (
	"fmt"
	"path"
	"sort"
	"strings"
)

// This file derives the label and the keywords from the members' own paths.
// There is no model here, which is exactly why HeuristicLabel exists as a
// separate field: a reader who is holding a heuristic label must be able to
// see that they are, and a reader who is later handed an enriched one must
// still be able to reach the heuristic it replaced.

// MaxKeywords caps the keyword list. Beyond a handful the terms stop
// discriminating between communities and start describing the language.
const MaxKeywords = 8

// minKeywordLength drops one-character path fragments, which are noise from
// split points rather than terms.
const minKeywordLength = 2

// heuristicLabel names a community from its members' paths, in three tiers,
// most specific first:
//
//  1. The longest directory prefix ALL members share, e.g. "src/parser/".
//     This is the strongest available statement and it is checked first.
//  2. Failing that, the directory holding the plurality of members, marked
//     with "/*" so it is visibly a plurality and not a shared root.
//  3. Failing that -- members scattered at the repository root -- the
//     lexicographically first member, with a count of the rest. It is a poor
//     label, and it is supposed to look like one.
//
// The label is a pure function of the sorted member list, so it is stable
// across runs and machines.
func heuristicLabel(members []string) string {
	if len(members) == 0 {
		return ""
	}
	if len(members) == 1 {
		return normalizeSlashes(members[0])
	}
	if prefix := commonDirPrefix(members); prefix != "" {
		return prefix + "/"
	}
	if dir, count := pluralityDir(members); dir != "" && count > 1 {
		return dir + "/*"
	}
	return fmt.Sprintf("%s +%d", normalizeSlashes(members[0]), len(members)-1)
}

// commonDirPrefix returns the longest directory prefix every member shares,
// without a trailing slash. It returns "" when the members share no directory.
func commonDirPrefix(members []string) string {
	var common []string
	for i, m := range members {
		segs := dirSegments(m)
		if i == 0 {
			common = segs
			continue
		}
		n := len(common)
		if len(segs) < n {
			n = len(segs)
		}
		k := 0
		for k < n && common[k] == segs[k] {
			k++
		}
		common = common[:k]
		if len(common) == 0 {
			return ""
		}
	}
	return strings.Join(common, "/")
}

// pluralityDir returns the directory holding the most members and how many.
// Ties break towards the lexicographically smallest directory, so the answer
// does not depend on iteration order.
func pluralityDir(members []string) (string, int) {
	counts := make(map[string]int)
	for _, m := range members {
		counts[strings.Join(dirSegments(m), "/")]++
	}
	dirs := make([]string, 0, len(counts))
	for d := range counts {
		dirs = append(dirs, d)
	}
	sort.Strings(dirs)
	best, bestN := "", 0
	for _, d := range dirs {
		if d == "" {
			continue
		}
		if counts[d] > bestN {
			best, bestN = d, counts[d]
		}
	}
	return best, bestN
}

// keywords ranks the path-derived terms of a community, most frequent first,
// ties broken lexicographically. A term is counted once per member, so a
// community of twenty files under "parser/" does not score "parser" twenty
// times for one directory.
func keywords(members []string) []string {
	counts := make(map[string]int)
	for _, m := range members {
		seen := make(map[string]bool)
		for _, term := range terms(m) {
			if seen[term] {
				continue
			}
			seen[term] = true
			counts[term]++
		}
	}
	list := make([]string, 0, len(counts))
	for t := range counts {
		list = append(list, t)
	}
	sort.Slice(list, func(i, j int) bool {
		if counts[list[i]] != counts[list[j]] {
			return counts[list[i]] > counts[list[j]]
		}
		return list[i] < list[j]
	})
	if len(list) > MaxKeywords {
		list = list[:MaxKeywords]
	}
	return list
}

// terms splits one path into lowercase alphanumeric fragments: every directory
// segment, the file stem and its extension, each further split on any
// non-alphanumeric character and on camelCase boundaries.
func terms(p string) []string {
	p = normalizeSlashes(p)
	var out []string
	for _, seg := range strings.Split(p, "/") {
		for _, dotted := range strings.Split(seg, ".") {
			for _, w := range splitWords(dotted) {
				if len(w) >= minKeywordLength {
					out = append(out, w)
				}
			}
		}
	}
	return out
}

// splitWords breaks an identifier into lowercase words on non-alphanumeric
// characters and on lower->upper transitions, so "parseNode" yields
// "parse", "node" and "parse_node" yields the same two.
func splitWords(s string) []string {
	var out []string
	var cur []rune
	flush := func() {
		if len(cur) > 0 {
			out = append(out, strings.ToLower(string(cur)))
			cur = cur[:0]
		}
	}
	prevLower := false
	for _, r := range s {
		isUpper := r >= 'A' && r <= 'Z'
		isLower := r >= 'a' && r <= 'z'
		isDigit := r >= '0' && r <= '9'
		if !isUpper && !isLower && !isDigit {
			flush()
			prevLower = false
			continue
		}
		if isUpper && prevLower {
			flush()
		}
		cur = append(cur, r)
		prevLower = isLower || isDigit
	}
	flush()
	return out
}

// dirSegments returns a path's directory segments, without the file name.
func dirSegments(p string) []string {
	dir := path.Dir(normalizeSlashes(p))
	if dir == "." || dir == "/" || dir == "" {
		return nil
	}
	return strings.Split(strings.Trim(dir, "/"), "/")
}

// normalizeSlashes makes a Windows-style path comparable to a POSIX one. The
// indexer stores forward slashes, but a caller reading paths from git on
// Windows may not, and a label that differs by separator would make two
// identical communities look different.
func normalizeSlashes(p string) string {
	return strings.ReplaceAll(p, "\\", "/")
}
