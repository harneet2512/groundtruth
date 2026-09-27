package parser

import (
	"sort"
	"testing"
)

func functionQualNames(result *ParseResult) []string {
	var out []string
	for _, n := range result.Nodes {
		if n.Label == "Function" || n.Label == "Method" {
			out = append(out, n.QualifiedName)
		}
	}
	sort.Strings(out)
	return out
}

func requireNames(t *testing.T, got []string, want ...string) {
	t.Helper()
	have := map[string]bool{}
	for _, name := range got {
		have[name] = true
	}
	for _, name := range want {
		if !have[name] {
			t.Fatalf("missing %q; have %v", name, got)
		}
	}
}

func TestNestedDefinitionsPython(t *testing.T) {
	result := parseDecoFixture(t, "pricing.py", ".py", `def make_formatter(prefix):
    def format_line(text):
        def pad(value):
            return " " + value
        return pad(prefix + text)
    check = lambda: format_line("x")
    return format_line
`)
	got := functionQualNames(result)
	requireNames(t, got, "make_formatter", "make_formatter.format_line", "make_formatter.format_line.pad")
	if len(got) != 3 {
		t.Fatalf("an unnamed lambda must not become a definition: %v", got)
	}
}

func TestNestedDefinitionsTypeScript(t *testing.T) {
	result := parseDecoFixture(t, "container.ts", ".ts", `export function createContainer(options: Options) {
  function isReady(name: string): boolean {
    return name.length > 0
  }
  const toKey = (name: string) => name.toLowerCase()
  items.forEach((item) => isReady(item))
  return { resolve(name: string) { return isReady(toKey(name)) } }
}
`)
	got := functionQualNames(result)
	requireNames(t, got, "createContainer", "createContainer.isReady", "createContainer.toKey")
	for _, name := range got {
		if name == "createContainer.item" || name == "createContainer." {
			t.Fatalf("callback argument named by guess: %v", got)
		}
	}
}

func TestNestedDefinitionsDoNotChangeCallAttribution(t *testing.T) {
	result := parseDecoFixture(t, "outer.py", ".py", `def outer():
    def inner():
        return helper()
    return inner()
`)
	for _, call := range result.Calls {
		if call.CalleeName == "helper" && call.CallerScope != "outer" {
			t.Fatalf("nested body calls stay attributed to the enclosing scope, got %q", call.CallerScope)
		}
	}
}
