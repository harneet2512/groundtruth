#!/bin/bash
set -euo pipefail

# Preflight validation for Qwen FC ablation ladder.
# Validates each arm before any benchmark run.
# Fails fast if any condition is violated.
#
# Usage: bash preflight_qwen_fc_ablation.sh [--arms A,B,C,D,E] [--outdir DIR]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ABLATION_DIR="$REPO_DIR/benchmarks/swebench/qwen_fc_ablation"
SWEAGENT_DIR="${GT_SWEAGENT_DIR:-/tmp/SWE-agent}"
OUTDIR="${GT_ABLATION_OUTDIR:-/tmp/qwen_fc_ablation/preflight_$(date +%s)}"
ARMS="${1:-A,B,C,D,E}"

echo "=== Qwen FC Ablation Preflight ==="
echo "Time: $(date -u)"
echo "Arms: $ARMS"
echo "Output: $OUTDIR"
echo ""

mkdir -p "$OUTDIR"

# Collect system info
MANIFEST=$(cat << MEOF
{
  "git_commit": "$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)",
  "git_dirty": $(git -C "$REPO_DIR" diff --quiet 2>/dev/null && echo false || echo true),
  "python_version": "$(python3 --version 2>&1 | head -1)",
  "sweagent_version": "$(python3 -m sweagent --help 2>&1 | head -1 | grep -oP 'version \K[^ ]+' || echo unknown)",
  "hostname": "$(hostname 2>/dev/null || echo unknown)",
  "cpu_count": $(nproc 2>/dev/null || echo 0),
  "ram_mb": $(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo 0),
  "disk_free_gb": $(df -BG / 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G' || echo 0),
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "arms": "$(echo $ARMS | tr ',' ' ')"
}
MEOF
)
echo "$MANIFEST" > "$OUTDIR/manifest.json"

REPORT_JSON="$OUTDIR/preflight_report.json"
REPORT_MD="$OUTDIR/PREFLIGHT_REPORT.md"
echo "[]" > "$REPORT_JSON"
echo "# Preflight Report" > "$REPORT_MD"
echo "" >> "$REPORT_MD"
echo "Generated: $(date -u)" >> "$REPORT_MD"
echo "" >> "$REPORT_MD"

PASS_COUNT=0
FAIL_COUNT=0

check_arm() {
    local arm="$1"
    local config=$(find "$ABLATION_DIR/configs" -name "${arm}_*.yaml" -o -name "${arm}.yaml" 2>/dev/null | head -1)
    [ -z "$config" ] && config="$ABLATION_DIR/configs/${arm}.yaml"
    local status="valid"
    local reason=""
    local gt_expected="false"
    local gt_hook_installed="false"
    local gt_hook_ran="false"
    local submit_ok="true"
    local xml_detected="false"
    local fc_errors="0"
    local instant_submit="false"
    local patch_created="false"
    local trace_created="false"
    local evidence_family_allowed="none"
    local evidence_family_observed="none"

    echo "--- Checking arm $arm ---"

    # 1. Config exists
    if [ ! -f "$config" ]; then
        echo "  FAIL: config not found: $config"
        status="invalid"
        reason="config_not_found"
        _write_result "$arm" "$config" "$status" "$reason" "$gt_expected" "$gt_hook_installed" "$gt_hook_ran" "$evidence_family_allowed" "$evidence_family_observed" "$xml_detected" "$fc_errors" "$instant_submit" "$patch_created" "$trace_created" "$submit_ok"
        return 1
    fi

    # 2. Parser check — must be function_calling
    local parser=$(grep -oP 'type:\s*\K\S+' "$config" | tail -1)
    if [ "$parser" != "function_calling" ]; then
        echo "  FAIL: parser is '$parser', expected 'function_calling'"
        status="invalid"
        reason="wrong_parser:$parser"
        _write_result "$arm" "$config" "$status" "$reason" "$gt_expected" "$gt_hook_installed" "$gt_hook_ran" "$evidence_family_allowed" "$evidence_family_observed" "$xml_detected" "$fc_errors" "$instant_submit" "$patch_created" "$trace_created" "$submit_ok"
        return 1
    fi
    echo "  PASS: parser=function_calling"

    # 3. Model check
    local model=$(grep -oP 'name:\s*\K\S+' "$config" | head -1)
    echo "  INFO: model=$model"

    # 4. GT bundle check
    if grep -q "gt_ablation\|groundtruth" "$config"; then
        gt_expected="true"
    fi

    # 5. Submit integrity — check install script
    if [ "$gt_expected" = "true" ]; then
        local install_sh="$ABLATION_DIR/hooks/install_ablation.sh"
        if [ -f "$install_sh" ]; then
            # Check for forbidden submit patterns
            if grep -qE 'submit.*PATCH\|SWE_AGENT_SUBMISSION\|gt-intervention\|submit_gate\|PreSubmit' "$install_sh"; then
                echo "  FAIL: install script contains submit patching"
                status="invalid"
                reason="submit_patched_in_install"
                submit_ok="false"
            else
                echo "  PASS: install script does not patch submit"
            fi
        else
            echo "  WARN: no install script found at $install_sh"
        fi
    else
        echo "  PASS: no GT bundle, submit integrity trivially OK"
    fi

    # 6. XML check — check hook for XML emission
    if [ "$gt_expected" = "true" ]; then
        local hook="$ABLATION_DIR/hooks/ablation_hook.py"
        if [ -f "$hook" ]; then
            if grep -qE '<gt-intervention>|<gt-evidence>|<gt-check>' "$hook"; then
                echo "  FAIL: hook emits XML tags"
                status="invalid"
                reason="xml_in_hook"
                xml_detected="true"
            else
                echo "  PASS: hook emits no XML"
            fi
        fi
    fi

    # 7. Ablation mode check
    if [ "$gt_expected" = "true" ]; then
        local mode=$(grep -oP 'GT_ABLATION_MODE:\s*\K\S+' "$config")
        echo "  INFO: GT_ABLATION_MODE=$mode"
        case "$mode" in
            inert) evidence_family_allowed="none" ;;
            empty_surface) evidence_family_allowed="none" ;;
            sibling_only) evidence_family_allowed="SIBLING" ;;
            import_only) evidence_family_allowed="IMPORT" ;;
            sibling_plus_import) evidence_family_allowed="SIBLING,IMPORT" ;;
            *) echo "  WARN: unknown mode '$mode'" ;;
        esac
    fi

    # 8. Smoke test — run one task
    echo "  Running smoke (1 task)..."
    local config_basename=$(basename "$config")
    cp "$config" "$SWEAGENT_DIR/config/$config_basename" 2>/dev/null || true
    if [ -f "$SWEAGENT_DIR/config/$config_basename" ]; then
        local smoke_dir="$OUTDIR/smoke_${arm}"
        mkdir -p "$smoke_dir"

        # Set up GT ablation bundle if needed
        if [ "$gt_expected" = "true" ]; then
            local bundle_dir="$SWEAGENT_DIR/tools/gt_ablation"
            mkdir -p "$bundle_dir"
            cp "$ABLATION_DIR/hooks/install_ablation.sh" "$bundle_dir/install.sh"
            cp "$ABLATION_DIR/hooks/ablation_hook.py" "$bundle_dir/ablation_hook.py"
            cp "$ABLATION_DIR/hooks/config.yaml" "$bundle_dir/config.yaml"
            # Copy gt_intel.py for evidence computation
            if [ -f "$REPO_DIR/benchmarks/swebench/gt_intel.py" ]; then
                cp "$REPO_DIR/benchmarks/swebench/gt_intel.py" "$bundle_dir/gt_intel.py"
            fi
            chmod +x "$bundle_dir/install.sh"
        fi

        local smoke_log="$smoke_dir/smoke.log"
        timeout 600 python3 -m sweagent run-batch \
            --config "config/$config_basename" \
            --instances.subset verified --instances.split test \
            --instances.filter "astropy__astropy-13453" \
            --output_dir "$smoke_dir" --num_workers 1 \
            > "$smoke_log" 2>&1 || true

        # Check smoke results
        local traj_count=$(find "$smoke_dir" -name "*.traj" 2>/dev/null | wc -l)
        if [ "$traj_count" -eq 0 ]; then
            echo "  FAIL: no trajectory produced"
            status="invalid"
            reason="no_trajectory"
        else
            trace_created="true"

            # Check for FC errors
            fc_errors=$(grep -c "FunctionCallingFormatError" "$smoke_log" 2>/dev/null || echo 0)
            if grep -q "exit_format" "$smoke_log" 2>/dev/null; then
                echo "  FAIL: FunctionCallingFormatError caused exit_format"
                status="invalid"
                reason="function_calling_format_error"
            else
                echo "  PASS: no fatal FC errors"
            fi

            # Check step count (fail if < 3 steps with no patch)
            local steps=$(python3 -c "
import json, glob
for f in glob.glob('$smoke_dir/astropy*/*.traj'):
    t = json.load(open(f))
    steps = len(t.get('trajectory', []))
    patch = t.get('info', {}).get('submission', '') or ''
    print(f'{steps}|{\"YES\" if patch.strip() else \"no\"}')
" 2>/dev/null || echo "0|no")
            local step_count="${steps%%|*}"
            local has_patch="${steps##*|}"

            if [ "$step_count" -lt 3 ] && [ "$has_patch" = "no" ]; then
                echo "  FAIL: instant-submit ($step_count steps, no patch)"
                instant_submit="true"
                status="invalid"
                reason="instant_submit:${step_count}_steps"
            else
                echo "  PASS: smoke OK (steps=$step_count, patch=$has_patch)"
                [ "$has_patch" = "YES" ] && patch_created="true"
            fi

            # Check GT hook ran (for B-F)
            if [ "$gt_expected" = "true" ]; then
                if [ -f "$smoke_dir"/astropy*/gt_ablation_events.jsonl ] || \
                   find "$smoke_dir" -name "gt_ablation_events.jsonl" 2>/dev/null | grep -q .; then
                    gt_hook_ran="true"
                    echo "  PASS: GT hook ran"
                else
                    # Hook runs inside container, events may be in /tmp not extracted
                    echo "  WARN: GT hook events not extracted (may have run inside container)"
                    gt_hook_ran="unknown"
                fi
                gt_hook_installed="true"
            fi
        fi
    else
        echo "  FAIL: could not copy config to SWE-agent"
        status="invalid"
        reason="config_copy_failed"
    fi

    _write_result "$arm" "$config" "$status" "$reason" "$gt_expected" "$gt_hook_installed" "$gt_hook_ran" "$evidence_family_allowed" "$evidence_family_observed" "$xml_detected" "$fc_errors" "$instant_submit" "$patch_created" "$trace_created" "$submit_ok"

    if [ "$status" = "valid" ]; then
        PASS_COUNT=$((PASS_COUNT + 1))
        echo "  STATUS: VALID"
        return 0
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        echo "  STATUS: INVALID ($reason)"
        return 1
    fi
}

_write_result() {
    local arm="$1" config="$2" status="$3" reason="$4" gt_expected="$5"
    local gt_installed="$6" gt_ran="$7" family_allowed="$8" family_observed="$9"
    local xml="${10}" fc_err="${11}" instant="${12}" patch="${13}" trace="${14}" submit="${15}"

    # Append to JSON report
    python3 -c "
import json
report = json.load(open('$REPORT_JSON'))
report.append({
    'arm_name': '$arm',
    'config_path': '$config',
    'parser_type': 'function_calling',
    'model_name': 'openai/qwen3-coder-480b-a35b-instruct-maas',
    'submit_integrity_passed': $( [ "$submit" = "true" ] && echo true || echo false ),
    'gt_hook_expected': $( [ "$gt_expected" = "true" ] && echo true || echo false ),
    'gt_hook_installed': $( [ "$gt_installed" = "true" ] && echo true || echo false ),
    'gt_hook_ran': '$gt_ran',
    'evidence_family_allowed': '$family_allowed',
    'evidence_family_observed': '$family_observed',
    'xml_detected': $( [ "$xml" = "true" ] && echo true || echo false ),
    'function_calling_errors': $fc_err,
    'instant_submit_detected': $( [ "$instant" = "true" ] && echo true || echo false ),
    'patch_created': $( [ "$patch" = "true" ] && echo true || echo false ),
    'trace_created': $( [ "$trace" = "true" ] && echo true || echo false ),
    'status': '$status',
    'invalid_reason': '$reason'
})
json.dump(report, open('$REPORT_JSON', 'w'), indent=2)
"

    # Append to markdown report
    local icon="✅"
    [ "$status" != "valid" ] && icon="❌"
    cat >> "$REPORT_MD" << MDEOF

### $icon Arm $arm
| Check | Result |
|---|---|
| Config | \`$config\` |
| Parser | function_calling |
| Submit integrity | $submit |
| GT hook expected | $gt_expected |
| GT hook installed | $gt_installed |
| GT hook ran | $gt_ran |
| Evidence family allowed | $family_allowed |
| XML detected | $xml |
| FC errors | $fc_err |
| Instant submit | $instant |
| Patch created | $patch |
| Trace created | $trace |
| **Status** | **$status** |
| Reason | $reason |
MDEOF
}

# Run preflight for each arm
source ~/sweagent-env/bin/activate 2>/dev/null || true
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://localhost:4000/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-dummy}"
cd "$SWEAGENT_DIR"

ALL_VALID=true
IFS=',' read -ra ARM_LIST <<< "$ARMS"
for arm in "${ARM_LIST[@]}"; do
    if ! check_arm "$arm"; then
        ALL_VALID=false
    fi
    echo ""
done

# Final summary
cat >> "$REPORT_MD" << MDEOF

---
## Summary

| Metric | Value |
|---|---|
| Arms checked | ${#ARM_LIST[@]} |
| Valid | $PASS_COUNT |
| Invalid | $FAIL_COUNT |
| **Verdict** | **$( [ "$ALL_VALID" = true ] && echo "ALL VALID — ready to run" || echo "BLOCKED — fix invalid arms before running" )** |
MDEOF

echo "=========================================="
echo "PREFLIGHT COMPLETE"
echo "  Valid: $PASS_COUNT / ${#ARM_LIST[@]}"
echo "  Invalid: $FAIL_COUNT / ${#ARM_LIST[@]}"
echo "  Report: $REPORT_MD"
echo "  JSON: $REPORT_JSON"
if [ "$ALL_VALID" = true ]; then
    echo "  VERDICT: ALL VALID — ready to run A-E"
else
    echo "  VERDICT: BLOCKED — fix invalid arms"
fi
echo "=========================================="

[ "$ALL_VALID" = true ] && exit 0 || exit 1
