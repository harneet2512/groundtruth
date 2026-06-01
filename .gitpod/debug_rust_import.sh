#!/usr/bin/env bash
set -e
cd /workspaces/groundtruth/gt-index

# Add debug prints to registerRustCrate and buildImportIndex
cat > /tmp/debug_patch.py << 'PYEOF'
import sys

f = sys.argv[1]
with open(f) as fh:
    lines = fh.readlines()

out = []
for i, line in enumerate(lines):
    out.append(line)
    # After "for fSlash := range seen {" — log what we found
    if 'for fSlash := range seen {' in line:
        out.append('\t\tlog.Printf("GT_DEBUG registerRustCrate: crate=%s dir=%s found=%d files", crateName, dir, len(seen))\n')
    # After each fm[...] = append in registerRustCrate
    if 'fm[crateName+"::"+colonPath]' in line and 'append' in line:
        out.append('\t\t\tlog.Printf("GT_DEBUG registered: %s -> %s", crateName+"::"+colonPath, fSlash)\n')
    if 'fm[crateName]' in line and 'append' in line and 'fSlash' in line:
        out.append('\t\t\tlog.Printf("GT_DEBUG registered: %s -> %s", crateName, fSlash)\n')
    # In buildImportIndex, after resolveModulePath
    if 'targetFiles := resolveModulePath' in line:
        out.append('\t\tif len(targetFiles) == 0 && strings.Contains(imp.ModulePath, "axum") { log.Printf("GT_DEBUG importIndex MISS: module=%s name=%s file=%s", imp.ModulePath, imp.ImportedName, imp.File) }\n')
        out.append('\t\tif len(targetFiles) > 0 && strings.Contains(imp.ModulePath, "axum") { log.Printf("GT_DEBUG importIndex HIT: module=%s name=%s -> %v", imp.ModulePath, imp.ImportedName, targetFiles) }\n')
    # In RegisterRustCratePaths, after glob expansion
    if 'memberDirs = append(memberDirs, filepath.ToSlash(rel))' in line:
        out.append('\t\t\t\t\t\t\tlog.Printf("GT_DEBUG glob expanded: %s", filepath.ToSlash(rel))\n')

with open(f, 'w') as fh:
    fh.writelines(out)
print(f"Patched {f} with debug prints")
PYEOF

python3 /tmp/debug_patch.py internal/resolver/resolver.go

# Need log import
if ! grep -q '"log"' internal/resolver/resolver.go; then
    sed -i 's|"path/filepath"|"log"\n\t"path/filepath"|' internal/resolver/resolver.go
fi

CGO_ENABLED=1 go build -o /tmp/gt-index-dbg ./cmd/gt-index/ && echo "BUILD_OK"
