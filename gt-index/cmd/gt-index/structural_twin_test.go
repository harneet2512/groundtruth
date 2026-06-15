package main

import "testing"

// Live-witness gap: the structural_twin detector (Consistency pillar source) was
// snake_case-only and DARK on all camelCase/PascalCase code (TS/JS, camelCase
// Go/Rust). Root cause was matchesTwinPair matching via strings.HasPrefix against
// prefixes carrying a trailing "_", so createUser/deleteUser never matched.
//
// These tests pin the generalized word-split behavior (snake + camel + Pascal),
// keep the snake_case path green (no regression), and lock the negative controls
// that the bare-verb-substring approach used to risk (created/deleted,
// createUser/createPost).

func TestSplitFirstWord(t *testing.T) {
	cases := []struct {
		name           string
		wantFirst      string
		wantRest       string
	}{
		{"create_user", "create", "user"}, // snake_case
		{"createUser", "create", "user"},  // camelCase
		{"CreateUser", "create", "user"},  // PascalCase
		{"createuser", "createuser", ""},  // no boundary
		{"get_value", "get", "value"},
		{"OpenFile", "open", "file"},
		{"created", "created", ""},        // single word, no boundary
		{"getUserById", "get", "userbyid"},
		{"_leading", "_leading", ""},      // underscore at idx 0 is not a boundary
	}
	for _, c := range cases {
		gotFirst, gotRest := splitFirstWord(c.name)
		if gotFirst != c.wantFirst || gotRest != c.wantRest {
			t.Errorf("splitFirstWord(%q) = (%q, %q); want (%q, %q)",
				c.name, gotFirst, gotRest, c.wantFirst, c.wantRest)
		}
	}
}

func TestMatchesTwinPair(t *testing.T) {
	cases := []struct {
		nameA     string
		nameB     string
		wantMatch bool
		wantType  string // checked only when wantMatch is true
	}{
		// camelCase — the live-witness gap (was DARK before the fix).
		{"createUser", "deleteUser", true, "create/delete"},
		// PascalCase.
		{"OpenFile", "CloseFile", true, "open/close"},
		// snake_case — must STILL match (no regression).
		{"get_value", "set_value", true, "get/set"},
		{"create_user", "delete_user", true, "create/delete"},
		// Order independence.
		{"deleteUser", "createUser", true, "create/delete"},
		// Mixed casing on the two names, same remainder.
		{"getUserById", "setUserById", true, "get/set"},

		// NEGATIVE: same verb, different remainder.
		{"createUser", "createPost", false, ""},
		// NEGATIVE: no remainder (the bare-verb-substring false-positive risk).
		{"created", "deleted", false, ""},
		// NEGATIVE: snake_case same verb, different remainder.
		{"get_value", "set_other", false, ""},
		// NEGATIVE: verbs not a configured pair.
		{"createUser", "openUser", false, ""},
		// NEGATIVE: remainder present on one side only.
		{"create", "deleteUser", false, ""},
	}
	for _, c := range cases {
		gotMatch, gotType := matchesTwinPair(c.nameA, c.nameB)
		if gotMatch != c.wantMatch {
			t.Errorf("matchesTwinPair(%q, %q) match = %v; want %v",
				c.nameA, c.nameB, gotMatch, c.wantMatch)
			continue
		}
		if c.wantMatch && gotType != c.wantType {
			t.Errorf("matchesTwinPair(%q, %q) type = %q; want %q",
				c.nameA, c.nameB, gotType, c.wantType)
		}
	}
}
