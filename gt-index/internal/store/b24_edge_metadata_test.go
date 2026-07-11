package store

import (
	"testing"
)

// B-24 (edge metadata normalization): edges.metadata is polymorphic — route JSON on API
// edges vs `;`-separated key=value on promoted CALLS. Consumers regex/LIKE it. A normalized,
// schema-versioned, individually-queryable surface (edge_metadata: edge_id, key, value,
// schema_version) derived through ONE canonical parser must let receiver_type / route /
// dataflow / usage be queried by key.
//
// RED on current code: the edge_metadata table + PopulateEdgeMetadata do not exist.
func TestB24_EdgeMetadataNormalizedQueryable(t *testing.T) {
	d := b22TestDB(t) // reuse the store test-db helper
	if _, err := d.db.Exec(
		`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		 (1,'Function','client','a.py','python'),
		 (2,'Function','handler','b.py','python'),
		 (3,'Function','caller','c.py','python'),
		 (4,'Function','callee','d.py','python')`,
	); err != nil {
		t.Fatal(err)
	}
	// Edge 1: API route edge — JSON metadata (as api_edges.go writes it).
	if _, err := d.db.Exec(
		`INSERT INTO edges (id, source_id, target_id, type, metadata)
		 VALUES (10, 1, 2, 'API_CALL', '{"route":"/orders","method":"GET"}')`,
	); err != nil {
		t.Fatal(err)
	}
	// Edge 2: promoted CALLS edge — legacy `;`-separated key=value metadata.
	if _, err := d.db.Exec(
		`INSERT INTO edges (id, source_id, target_id, type, metadata)
		 VALUES (20, 3, 4, 'CALLS', 'receiver_type=UserRepo;dataflow=fetch;usage=return')`,
	); err != nil {
		t.Fatal(err)
	}

	if err := d.PopulateEdgeMetadata(); err != nil {
		t.Fatalf("PopulateEdgeMetadata: %v", err)
	}

	get := func(edgeID int64, key string) string {
		var v string
		d.db.QueryRow(`SELECT value FROM edge_metadata WHERE edge_id = ? AND key = ?`, edgeID, key).Scan(&v)
		return v
	}
	// The polymorphic JSON edge is now queryable by key.
	if got := get(10, "route"); got != "/orders" {
		t.Errorf("edge 10 route=%q want /orders", got)
	}
	if got := get(10, "method"); got != "GET" {
		t.Errorf("edge 10 method=%q want GET", got)
	}
	// The polymorphic key=value edge is now queryable by key — no regex/LIKE.
	if got := get(20, "receiver_type"); got != "UserRepo" {
		t.Errorf("edge 20 receiver_type=%q want UserRepo", got)
	}
	if got := get(20, "dataflow"); got != "fetch" {
		t.Errorf("edge 20 dataflow=%q want fetch", got)
	}
	if got := get(20, "usage"); got != "return" {
		t.Errorf("edge 20 usage=%q want return", got)
	}
	// A schema version is recorded so the format is self-describing.
	var ver int
	if err := d.db.QueryRow(`SELECT schema_version FROM edge_metadata WHERE edge_id = 20 AND key = 'dataflow'`).Scan(&ver); err != nil {
		t.Fatalf("schema_version not readable: %v", err)
	}
	if ver < 1 {
		t.Errorf("schema_version=%d want >= 1", ver)
	}
}

// B-24 canonical parser unit: ParseEdgeMetadata is the ONE parser for both stored formats.
func TestB24_ParseEdgeMetadataBothFormats(t *testing.T) {
	json := ParseEdgeMetadata(`{"route":"/x","method":"POST"}`)
	if json["route"] != "/x" || json["method"] != "POST" {
		t.Errorf("JSON parse wrong: %v", json)
	}
	kv := ParseEdgeMetadata(`receiver_type=Foo;dataflow=bar;usage=call`)
	if kv["receiver_type"] != "Foo" || kv["dataflow"] != "bar" || kv["usage"] != "call" {
		t.Errorf("key=value parse wrong: %v", kv)
	}
	if len(ParseEdgeMetadata("")) != 0 {
		t.Errorf("empty metadata must parse to empty map")
	}
}

// Graph-F10 (bounce 2026-07-10): pin ParseEdgeMetadata's JSON-scalar rendering AND document
// the Go/Python big-integer divergence. Go decodes JSON numbers to float64 then int64, so an
// integer beyond float64's 2^53 exact-integer mantissa loses precision here, whereas the
// Python twin (curation_map._json_scalar_str) keeps arbitrary precision via str(int). This
// does NOT bite today — every real metadata key (receiver_type/route/method/dataflow/usage)
// is a STRING and the `;`-separated key=value form never numeric-coerces — but the boundary
// is pinned so a regression, or a future numeric key, is caught rather than silently
// diverging. (INFO-level per the finding; no behavior change, characterization only.)
func TestParseEdgeMetadataScalarParity(t *testing.T) {
	cases := []struct{ name, raw, key, want string }{
		{"small_int", `{"n":42}`, "n", "42"},              // matches Python str(int)
		{"bool_true", `{"b":true}`, "b", "true"},
		{"bool_false", `{"b":false}`, "b", "false"},
		{"integer_float_no_dot", `{"g":2.0}`, "g", "2"},   // integral float -> no ".0"
		{"fractional_float", `{"f":1.5}`, "f", "1.5"},
		{"string_passthrough", `{"s":"UserRepo"}`, "s", "UserRepo"},
		// KNOWN DIVERGENCE (pinned): 2^53+1 = 9007199254740993 is not exactly float64-
		// representable, so Go rounds to 9007199254740992; Python str(int) keeps the exact
		// value. Non-biting (metadata keys are non-numeric); pinned to catch drift.
		{"big_int_precision_loss", `{"big":9007199254740993}`, "big", "9007199254740992"},
	}
	for _, c := range cases {
		if got := ParseEdgeMetadata(c.raw)[c.key]; got != c.want {
			t.Errorf("%s: ParseEdgeMetadata(%q)[%q] = %q, want %q", c.name, c.raw, c.key, got, c.want)
		}
	}
	// The `;`-separated key=value form keeps values as OPAQUE strings (never numeric-
	// coerced), so a big numeric-LOOKING value round-trips verbatim — parity with Python.
	kv := ParseEdgeMetadata("receiver_type=UserRepo;count=9007199254740993")
	if kv["count"] != "9007199254740993" {
		t.Errorf("key=value numeric-looking value must pass through verbatim, got %q", kv["count"])
	}
}
