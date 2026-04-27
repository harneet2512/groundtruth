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
    local config=$(find "$ABLATION_DIR/configs_v2" "$ABLATION_DIR/configs" -name "${arm}_*.yaml" -o -name "${arm}.yaml" 2>/dev/null | head -1)
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
    local parser=$(grep -A1 'parse_function' "$config" | grep 'type:' | grep -oP 'type:\s*\K\S+')
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
    if grep -q "gt_ablation\|groundtruth" "$config" 2>/dev/null; then
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

        # Set up GT ablation v2 bundle — uses REAL proven hook
        if [ "$gt_expected" = "true" ]; then
            local VM_BUNDLE="$REPO_DIR/benchmarks/swebench/vm_bundle"
            local bundle_dir="$SWEAGENT_DIR/tools/gt_ablation_v2"
            rm -rf "$bundle_dir"
            mkdir -p "$bundle_dir/bin"
            # Use noindex install for arm B, full install for C-F
            if [ "$arm" = "B" ] && [ -f "$VM_BUNDLE/install_fc_noindex.sh" ]; then
                cp "$VM_BUNDLE/install_fc_noindex.sh" "$bundle_dir/install.sh"
            else
                cp "$VM_BUNDLE/install_fc.sh" "$bundle_dir/install.sh"
            fi
            echo "tools: {}" > "$bundle_dir/config.yaml"
            cp "$VM_BUNDLE/swe_agent_state_gt.py" "$bundle_dir/bin/swe_agent_state_gt.py"
            cp "$REPO_DIR/benchmarks/swebench/gt_intel.py" "$bundle_dir/bin/gt_intel.py"
            for f in lsp_promoter.py gt_review_patch.py gt_canary_report.py gt_metrics.py; do
                [ -f "$VM_BUNDLE/$f" ] && cp "$VM_BUNDLE/$f" "$bundle_dir/bin/$f"
            done
            echo '#!/bin/bash' > "$bundle_dir/bin/_noop"
            chmod +x "$bundle_dir/install.sh" "$bundle_dir/bin/"*
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

            # ════════════════════════════════════════════════════
            # DEEP SCAFFOLD CHECKS — trajectory-level inspection
            # ════════════════════════════════════════════════════
            echo "  --- Deep scaffold checks ---"
            local traj_file=$(find "$smoke_dir" -name "*.traj" | head -1)
            if [ -n "$traj_file" ] && [ -f "$traj_file" ]; then
                local deep_result=$(python3 << DEEPEOF
import json, sys

traj = json.load(open("$traj_file"))
trajectory = traj.get("trajectory", [])
info = traj.get("info", {})
history = traj.get("history", [])

issues = []
warnings = []

# 1. XML in observations — check every observation for XML control tags
xml_in_obs = 0
for step in trajectory:
    obs = step.get("observation", "") or ""
    for tag in ["<gt-intervention", "<gt-evidence>", "<gt-check>", "<<SWE_AGENT_SUBMISSION>>"]:
        if tag in obs:
            xml_in_obs += 1
            break
if xml_in_obs > 0:
    issues.append(f"XML_IN_OBSERVATIONS: {xml_in_obs} steps had XML control tags in observations")

# 2. gt_evidence in observations when NOT expected
gt_expected = "$gt_expected" == "true"
gt_evidence_count = 0
for step in trajectory:
    obs = step.get("observation", "") or ""
    if "gt_evidence" in obs or ("gt-evidence" in obs.lower()):
        gt_evidence_count += 1
if not gt_expected and gt_evidence_count > 0:
    issues.append(f"GT_EVIDENCE_LEAK: {gt_evidence_count} observations contained GT evidence on baseline arm")
if gt_expected and "$arm" in ("B", "C") and gt_evidence_count > 0:
    issues.append(f"GT_EVIDENCE_LEAK: {gt_evidence_count} observations had evidence on inert/empty arm")

# 3. FunctionCallingFormatError count (non-fatal requeries)
fc_requery = 0
for step in trajectory:
    obs = step.get("observation", "") or ""
    if "FunctionCallingFormatError" in obs or "did not use any tool calls" in obs:
        fc_requery += 1
if fc_requery > 0:
    warnings.append(f"FC_REQUERY: {fc_requery} non-fatal function_calling format retries")
if fc_requery > len(trajectory) * 0.3:
    issues.append(f"FC_REQUERY_HIGH: {fc_requery}/{len(trajectory)} steps had format retries (>30%)")

# 4. Tool call analysis — verify function_calling is working
tool_calls = 0
text_only = 0
for step in trajectory:
    action = step.get("action", "") or ""
    if step.get("tool_calls") or "tool_calls" in str(step.get("response", {})):
        tool_calls += 1
    # In function_calling mode, actions should be tool call results
    # Check if any step has raw bash-style fenced code blocks (thought_action leak)
    if "\`\`\`" in action and "bash" in action.lower():
        text_only += 1
if text_only > len(trajectory) * 0.5:
    warnings.append(f"THOUGHT_ACTION_LEAK: {text_only}/{len(trajectory)} steps look like thought_action format")

# 5. Bootstrap health — check first 5 steps for abnormal patterns
early_steps = trajectory[:5]
empty_early = sum(1 for s in early_steps if not (s.get("action", "") or "").strip())
if empty_early >= 3:
    issues.append(f"BOOTSTRAP_FAILURE: {empty_early}/5 early steps had empty actions")

# 6. Submit behavior — check if submit was called and how
submit_count = 0
submit_blocked = 0
for step in trajectory:
    action = step.get("action", "") or ""
    obs = step.get("observation", "") or ""
    if "submit" in action.lower():
        submit_count += 1
    if "Submission blocked" in obs or "gt-intervention" in obs:
        submit_blocked += 1
if submit_blocked > 0:
    issues.append(f"SUBMIT_BLOCKED: {submit_blocked} submit attempts were blocked by GT gate")

# 7. Step distribution health
step_count = len(trajectory)
if step_count <= 4:
    issues.append(f"STEP_COUNT_LOW: only {step_count} steps — possible scaffold failure")
elif step_count >= 145:
    warnings.append(f"STEP_COUNT_HIGH: {step_count} steps — hit step limit")

# 8. Observation size — check for context bloat
obs_sizes = [len(s.get("observation", "") or "") for s in trajectory]
avg_obs = sum(obs_sizes) / max(len(obs_sizes), 1)
max_obs = max(obs_sizes) if obs_sizes else 0
if max_obs > 50000:
    warnings.append(f"OBS_SIZE_LARGE: max observation {max_obs} chars (context bloat risk)")
if avg_obs > 10000:
    warnings.append(f"OBS_AVG_LARGE: avg observation {int(avg_obs)} chars")

# 9. Repeated identical actions (agent stuck)
actions = [s.get("action", "") for s in trajectory]
repeated = 0
for i in range(1, len(actions)):
    if actions[i] and actions[i] == actions[i-1]:
        repeated += 1
if repeated > 5:
    warnings.append(f"REPEATED_ACTIONS: {repeated} consecutive identical action pairs (stuck agent)")

# Output
result = {"issues": issues, "warnings": warnings, "step_count": step_count,
          "xml_in_obs": xml_in_obs, "gt_evidence_count": gt_evidence_count,
          "fc_requery": fc_requery, "submit_blocked": submit_blocked,
          "avg_obs_chars": int(avg_obs), "max_obs_chars": max_obs}
print(json.dumps(result))
DEEPEOF
)
                # Parse deep check results
                local deep_issues=$(echo "$deep_result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('issues',[])))" 2>/dev/null || echo "?")
                local deep_warnings=$(echo "$deep_result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('warnings',[])))" 2>/dev/null || echo "?")

                # Print issues
                echo "$deep_result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for i in d.get('issues', []):
    print(f'  FAIL (deep): {i}')
for w in d.get('warnings', []):
    print(f'  WARN (deep): {w}')
if not d.get('issues') and not d.get('warnings'):
    print('  PASS: deep scaffold checks clean')
print(f'  INFO (deep): steps={d[\"step_count\"]}, fc_requery={d[\"fc_requery\"]}, xml_in_obs={d[\"xml_in_obs\"]}, gt_evidence={d[\"gt_evidence_count\"]}, submit_blocked={d[\"submit_blocked\"]}, avg_obs={d[\"avg_obs_chars\"]}ch, max_obs={d[\"max_obs_chars\"]}ch')
" 2>/dev/null || echo "  WARN: deep check parse failed"

                # Fail on issues (not warnings)
                if [ "$deep_issues" != "0" ] && [ "$deep_issues" != "?" ]; then
                    local first_issue=$(echo "$deep_result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['issues'][0] if d.get('issues') else '')" 2>/dev/null)
                    if [ -n "$first_issue" ]; then
                        status="invalid"
                        reason="deep_scaffold:$first_issue"
                    fi
                fi

                # Save deep result
                echo "$deep_result" > "$smoke_dir/deep_scaffold_check.json"
            else
                echo "  SKIP: no trajectory for deep checks"
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
    'submit_integrity_passed': '$submit' == 'true',
    'gt_hook_expected': '$gt_expected' == 'true',
    'gt_hook_installed': '$gt_installed' == 'true',
    'gt_hook_ran': '$gt_ran',
    'evidence_family_allowed': '$family_allowed',
    'evidence_family_observed': '$family_observed',
    'xml_detected': '$xml' == 'true',
    'function_calling_errors': int('$fc_err' or '0'),
    'instant_submit_detected': '$instant' == 'true',
    'patch_created': '$patch' == 'true',
    'trace_created': '$trace' == 'true',
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
