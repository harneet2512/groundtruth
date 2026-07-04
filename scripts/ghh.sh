#!/usr/bin/env bash
# ghh — run `gh` AS harneet2512 (the admin account) regardless of which account is
# currently "active" in the shared keyring. The two parallel sessions fight over
# `gh auth switch` (global state), causing HTTP 403 on workflow dispatch. This wrapper
# overrides the token PER-INVOCATION via GH_TOKEN, so it never switches the global
# active account and never disturbs the other session.
#
# Usage:  bash scripts/ghh.sh workflow run swebench_300task.yml -R harneet2512/groundtruth ...
#         bash scripts/ghh.sh run list -R harneet2512/groundtruth ...
_tok="$(gh auth token --user harneet2512 2>/dev/null)"
if [ -z "$_tok" ]; then
  echo "ghh: no harneet2512 token in keyring (gh auth login --user harneet2512)" >&2
  exit 1
fi
exec env GH_TOKEN="$_tok" gh "$@"
