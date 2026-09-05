# Pinned TypeScript dialect bindings

Vendored from github.com/smacker/go-tree-sitter at
v0.0.0-20240625050157-a31a98a7c0f6 (the existing runtime pin).
The generated parser tables, parser headers, scanner entry points and Go
bindings are unchanged. The only grammar correction is in both scanner.h files:
`<` uses the existing type-versus-expression automatic-semicolon rule already
used for `(` and `[`. This permits adjacent newline-separated generic call
signatures without terminating expression comparisons or shifts.

Original generated parser SHA-256:

- typescript/parser.c: 9af9b9b1303c919d8bd28e11fd27939751682aba1211ca76b9fbcdb0833e40d6
- tsx/parser.c: b17c6dfe7a0f8f7ecd4018cdf75edf3d955620e67a892f6f3c7a0fb4f988ee03

The specs registry selects plain TypeScript for .ts and TSX for .tsx, retaining
the canonical language name `typescript` for both. Regression tests live in
internal/specs/typescript_test.go. An unstamped development build is not a
certified shipping artifact; normal producer build/provenance gates still apply.
