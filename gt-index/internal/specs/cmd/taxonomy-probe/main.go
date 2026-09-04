// taxonomy-probe prints the language x kind matrix of the symbol taxonomy,
// read from the registered mappings themselves. The matrix in REPORT.md is
// this program's output, so it cannot drift from the code the way a hand-typed
// table does.
//
// Cells:
//
//	N   the kind is present, mapped to N tree-sitter node types
//	P   GT already records the fact as a typed property row
//	.   the grammar has no such construct; -reasons prints why
//
// Usage:
//
//	go run -tags sqlite_fts5 ./internal/specs/cmd/taxonomy-probe
//	go run -tags sqlite_fts5 ./internal/specs/cmd/taxonomy-probe -reasons
package main

import (
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
)

func main() {
	reasons := flag.Bool("reasons", false, "print the recorded reason for every absence")
	flag.Parse()

	langs := make([]string, 0, len(specs.Taxonomies))
	for name := range specs.Taxonomies {
		langs = append(langs, name)
	}
	sort.Strings(langs)

	if *reasons {
		printReasons(langs)
		return
	}
	printMatrix(langs)
}

// presentCounts returns, for one language, kind -> number of distinct
// tree-sitter node types that produce it.
func presentCounts(tx *specs.Taxonomy) map[string]int {
	out := map[string]int{}
	for _, kind := range tx.ByNodeType {
		out[kind]++
	}
	for _, ck := range tx.ChildKinds {
		out[ck.Kind]++
	}
	for _, kw := range tx.Keywords {
		out[kw.Kind]++
	}
	for _, d := range tx.Decls {
		out[d.Kind]++
	}
	for _, m := range tx.MemberDecls {
		out[m.Kind]++
	}
	if len(tx.ConstructorNames) > 0 {
		out[specs.KindConstructor]++
	}
	for _, k := range specs.UniversalKinds {
		out[k]++
	}
	return out
}

func printMatrix(langs []string) {
	w := os.Stdout
	fmt.Fprintf(w, "| language | %s |\n", strings.Join(specs.AllSymbolKinds, " | "))
	fmt.Fprintf(w, "|---|%s\n", strings.Repeat("---|", len(specs.AllSymbolKinds)))
	for _, lang := range langs {
		tx := specs.Taxonomies[lang]
		counts := presentCounts(tx)
		cells := make([]string, 0, len(specs.AllSymbolKinds))
		for _, kind := range specs.AllSymbolKinds {
			switch {
			case counts[kind] > 0:
				cells = append(cells, fmt.Sprintf("%d", counts[kind]))
			case tx.AsProperty[kind] != "":
				cells = append(cells, "P")
			default:
				cells = append(cells, ".")
			}
		}
		fmt.Fprintf(w, "| %s | %s |\n", lang, strings.Join(cells, " | "))
	}
	fmt.Fprintf(w, "\n%d languages, %d kinds. N = present via N node types, P = already a typed property row, . = absent with a recorded reason (-reasons).\n",
		len(langs), len(specs.AllSymbolKinds))
}

func printReasons(langs []string) {
	w := os.Stdout
	for _, lang := range langs {
		tx := specs.Taxonomies[lang]
		kinds := make([]string, 0, len(tx.Absent))
		for kind := range tx.Absent {
			kinds = append(kinds, kind)
		}
		sort.Strings(kinds)
		fmt.Fprintf(w, "\n== %s ==\n", lang)
		for _, kind := range kinds {
			fmt.Fprintf(w, "  %-12s %s\n", kind, tx.Absent[kind])
		}
	}
}
