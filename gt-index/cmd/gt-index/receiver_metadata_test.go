package main

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
)

// receiverEdgeMetadata encoding guard (W-B bounce hardening): a ReceiverType that
// cannot be encoded as ONE `;`-separated key=value segment (contains `;`, `=`, or
// whitespace) must be dropped entirely — a `;`/`=` inside the value would fabricate
// a fake metadata key (e.g. a spoofed `dataflow=`) for every LIKE/instr reader.
// Clean class names encode; empty stays empty (byte-identical-off).
func TestReceiverEdgeMetadataEncodingGuard(t *testing.T) {
	cases := []struct {
		name string
		recv string
		want string
	}{
		{"clean class name", "HttpClient", "receiver_type=HttpClient"},
		{"empty (receiver-blind)", "", ""},
		{"semicolon injects a segment", "Foo;dataflow=evil", ""},
		{"equals fabricates a key", "Foo=Bar", ""},
		{"space", "Foo Bar", ""},
		{"tab", "Foo\tBar", ""},
		{"newline", "Foo\nBar", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := receiverEdgeMetadata(resolver.ResolvedCall{ReceiverType: tc.recv})
			if got != tc.want {
				t.Errorf("receiverEdgeMetadata(ReceiverType=%q) = %q; want %q", tc.recv, got, tc.want)
			}
		})
	}
}
