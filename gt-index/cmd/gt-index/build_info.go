package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"runtime"
	"sort"
	"strings"
)

const buildInfoSchema = "gt-index.build.v1"

type buildIdentity struct {
	Schema            string   `json:"schema"`
	Complete          bool     `json:"complete"`
	GitCommit         string   `json:"git_commit"`
	BuildTimeUTC      string   `json:"build_time_utc"`
	SourceFingerprint string   `json:"source_fingerprint"`
	GoToolchain       string   `json:"go_toolchain"`
	BuildTags         string   `json:"build_tags"`
	BuildID           string   `json:"build_id"`
	ExecutableSHA256  string   `json:"executable_sha256"`
	SchemaVersion     string   `json:"graph_schema_version"`
	Capabilities      []string `json:"capabilities"`
}

func currentBuildIdentity() (buildIdentity, error) {
	executable, err := os.Executable()
	if err != nil {
		return buildIdentity{}, fmt.Errorf("locate executable: %w", err)
	}
	bytes, err := os.ReadFile(executable)
	if err != nil {
		return buildIdentity{}, fmt.Errorf("hash executable: %w", err)
	}
	executableSum := sha256.Sum256(bytes)
	toolchain := goToolchain
	if toolchain == "" || toolchain == "unknown" {
		toolchain = runtime.Version()
	}
	capabilities := []string{"atomic_graph_publication", "call_resolution_v2", "framework_surface_resolution_v1", "incremental_stale_suppression", "parse_failure_accounting", "retained_call_candidates", "versioned_query_policy"}
	sort.Strings(capabilities)
	identityMaterial := strings.Join([]string{commitSHA, buildTimeUTC, sourceFingerprint, toolchain, compiledBuildTags, schemaVersion, strings.Join(capabilities, ",")}, "\x00")
	buildSum := sha256.Sum256([]byte(identityMaterial))
	return buildIdentity{
		Schema: buildInfoSchema, Complete: commitSHA != "" && commitSHA != "unknown" && buildTimeUTC != "" && buildTimeUTC != "unknown" && sourceFingerprint != "" && sourceFingerprint != "unknown" && compiledBuildTags != "" && compiledBuildTags != "unknown",
		GitCommit: commitSHA, BuildTimeUTC: buildTimeUTC, SourceFingerprint: sourceFingerprint, GoToolchain: toolchain,
		BuildTags: compiledBuildTags, BuildID: hex.EncodeToString(buildSum[:]), ExecutableSHA256: hex.EncodeToString(executableSum[:]),
		SchemaVersion: schemaVersion, Capabilities: capabilities,
	}, nil
}

func writeBuildInfo(w io.Writer) error {
	identity, err := currentBuildIdentity()
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(w)
	encoder.SetEscapeHTML(false)
	return encoder.Encode(identity)
}
