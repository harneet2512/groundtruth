package specs

import (
	"github.com/smacker/go-tree-sitter/elixir"
)

func init() {
	// KNOWN LIMITATION: Elixir indexing produces unreliable results.
	// The tree-sitter Elixir grammar represents def/defmodule/import/alias/use
	// all as generic "call" nodes. With everything mapping to "call" AND BodyField
	// empty, the parser cannot distinguish function definitions from class definitions
	// from call expressions from import statements. This means:
	//   - Functions/classes may not be extracted correctly
	//   - Import/call overlap causes the same dispatch collision as Ruby/Lua
	//   - Empty BodyField prevents function body extraction for calls/properties
	// Elixir support requires a custom extraction pass, not the generic spec pipeline.
	Register(&Spec{
		Name:       "elixir",
		Extensions: []string{".ex", ".exs"},
		Language:   elixir.GetLanguage(),

		FunctionNodes: []string{"call"},
		ClassNodes:    []string{"call"},
		CallNodes:     []string{"call"},
		ImportNodes:   []string{"call"},

		TestFuncPattern: `^test_`,

		NameField:   "",
		BodyField:   "",
		ParamsField: "arguments",

		IsExported: func(name string) bool {
			return true
		},
	})
}
