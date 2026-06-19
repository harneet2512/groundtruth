# GroundTruth — Codebase Validation

Before writing or editing code files, call `groundtruth_generate` with your intended code.
Use the corrected version from the response in your Write/Edit calls.

After writing code, call `groundtruth_validate` to verify the written file.

This ensures all imports, function calls, and type references point to symbols that actually exist in the codebase.
