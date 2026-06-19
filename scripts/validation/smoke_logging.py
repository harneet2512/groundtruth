import os
import json
import sys
from unittest.mock import MagicMock
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from scripts.swebench.oh_gt_full_wrapper import (
    patched_initialize_runtime,
    patched_get_instruction,
    GTRuntimeConfig,
    HookEvent
)

def test_interaction_logging():
    print("Starting Interaction Logging Smoke Test...")
    
    # Real object for runtime
    class FakeRuntime:
        def __init__(self):
            self.actions_run = []
        def run_action(self, action):
            self.actions_run.append(action)
            obs = MagicMock()
            obs.content = "Observation for " + str(getattr(action, "command", "action"))
            return obs
            
    runtime = FakeRuntime()
    instance = {
        "instance_id": "test_task_123",
        "problem_statement": "Fix the bug in main.py",
        "base_commit": "abcdef123"
    }
    metadata = MagicMock()
    metadata.max_iterations = 10
    
    # 1. Initialize Runtime
    print("Testing patched_initialize_runtime...")
    # We need to set the global _ORIG_INITIALIZE_RUNTIME to a NOOP
    import scripts.swebench.oh_gt_full_wrapper as wrapper
    wrapper._ORIG_INITIALIZE_RUNTIME = lambda r, i, m: None
    wrapper._ORIG_GET_INSTRUCTION = lambda i, m: MagicMock(content="Original Instruction")
    
    # Mock install_graph_and_hook to avoid actual Docker/git calls during smoke test
    wrapper.install_graph_and_hook = MagicMock(return_value=["gt_query"])
    # Mock brief generation to return a fixed brief
    wrapper._upload_bytes_b64 = MagicMock()
    wrapper._run_internal = MagicMock(return_value="GT deterministic edit plan (ranked):\n1. main.py [primary source]\n---GT_L2_JSON---\n{}")
    
    patched_initialize_runtime(runtime, instance, metadata)
    
    config = getattr(runtime, "_gt_full_config", None)
    print(f"DEBUG: runtime._gt_full_config = {config}")
    if config:
        print(f"DEBUG: config.instance_ref = {config.instance_ref}")
        print(f"DEBUG: instance = {instance}")
    
    assert config is not None
    assert config.instance_ref == instance
    
    # 2. Get Instruction (Injects L1 Brief and Logs it)
    print("Testing patched_get_instruction (L1 Logging)...")
    msg = patched_get_instruction(instance, metadata)
    
    # Check if interaction was logged in config
    assert len(config.interaction_log) > 0
    l1_entry = config.interaction_log[0]
    assert l1_entry["layer"] == "L1"
    assert l1_entry["type"] == "brief_injection"
    
    # Check if immediate flush to instance worked
    assert "gt_interactions" in instance
    assert len(instance["gt_interactions"]) == 1
    assert instance["gt_interactions"][0]["layer"] == "L1"
    
    print("L1 Interaction logged and flushed successfully.")
    
    # 3. Test L3/L3b Logging (Simulate a View/Edit)
    print("Testing patched_run_action (L3 Logging)...")
    # Simulate a FileViewAction
    action = MagicMock()
    type(action).__name__ = "FileViewAction"
    action.path = "main.py"
    
    # We need to mock classify_tool_event to return post_view
    wrapper.classify_tool_event = MagicMock(return_value=HookEvent(kind="post_view", path="main.py"))
    # Mock make_view_hook_command to return a dummy cmd
    wrapper.make_view_hook_command = MagicMock(return_value="echo evidence")
    # Mock _run_internal for the hook
    wrapper._run_internal = MagicMock(return_value="[GT_CHANGE] Evidence found.")
    
    runtime.run_action(action)
    
    # Check if L3b interaction was logged
    assert len(config.interaction_log) == 2
    l3b_entry = config.interaction_log[1]
    assert l3b_entry["layer"] == "L3b"
    assert "Evidence" in l3b_entry["gt_sent_preview"]
    
    # Check if instance is updated
    assert len(instance["gt_interactions"]) == 2
    
    print("L3b Interaction logged and flushed successfully.")
    
    print("\nSMOKE TEST PASSED: Interaction logging flush is verified.")

if __name__ == "__main__":
    try:
        test_interaction_logging()
    except Exception as e:
        print(f"SMOKE TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
