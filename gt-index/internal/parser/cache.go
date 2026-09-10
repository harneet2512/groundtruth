package parser

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"

	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

const parseCacheSchema = "gt.parse-cache.v1"

// ParseCache stores complete parser-local results, before database ID remapping.
// Every load decodes a fresh object: callers may mutate node IDs without changing
// subsequent cache hits. Invalid or unavailable cache entries are ordinary misses.
type ParseCache struct {
	Root                string
	ProducerFingerprint string
}

type parseCacheEntry struct {
	Key    string          `json:"key"`
	Digest string          `json:"digest"`
	Result json.RawMessage `json:"result"`
}

func (c ParseCache) ParseFile(sf walker.SourceFile, isTest bool) (*ParseResult, bool, error) {
	src, err := os.ReadFile(sf.AbsPath)
	if err != nil {
		return nil, false, err
	}
	if c.Root == "" || c.ProducerFingerprint == "" || c.ProducerFingerprint == "unknown" {
		result, err := ParseBytes(sf, isTest, src)
		return result, false, err
	}
	sourceHash := sha256.Sum256(src)
	material, err := json.Marshal([]any{parseCacheSchema, c.ProducerFingerprint,
		filepath.ToSlash(sf.Path), sf.Language, isTest, hex.EncodeToString(sourceHash[:])})
	if err != nil {
		return nil, false, err
	}
	keyHash := sha256.Sum256(material)
	key := hex.EncodeToString(keyHash[:])
	path := filepath.Join(c.Root, key[:2], key+".json")
	if payload, err := os.ReadFile(path); err == nil {
		var entry parseCacheEntry
		if json.Unmarshal(payload, &entry) == nil && entry.Key == key {
			digest := sha256.Sum256(entry.Result)
			var result ParseResult
			if hex.EncodeToString(digest[:]) == entry.Digest &&
				json.Unmarshal(entry.Result, &result) == nil && string(entry.Result) != "null" {
				return &result, true, nil
			}
		}
	}
	result, err := ParseBytes(sf, isTest, src)
	if err != nil {
		return result, false, err
	}
	raw, err := json.Marshal(result)
	if err != nil {
		return result, false, nil
	}
	digest := sha256.Sum256(raw)
	payload, err := json.Marshal(parseCacheEntry{key, hex.EncodeToString(digest[:]), raw})
	if err != nil {
		return result, false, nil
	}
	if os.MkdirAll(filepath.Dir(path), 0700) != nil {
		return result, false, nil
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".parse-*")
	if err != nil {
		return result, false, nil
	}
	defer os.Remove(f.Name())
	_, writeErr := f.Write(payload)
	syncErr := f.Sync()
	closeErr := f.Close()
	if writeErr == nil && syncErr == nil && closeErr == nil {
		_ = os.Rename(f.Name(), path)
	}
	return result, false, nil
}
