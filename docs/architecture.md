# Architecture

## Product Slices

Overleaf Sync is easier to reason about when split by user task, not by protocol detail.

1. Authentication
   - Log in once.
   - Reuse the auth store automatically.

2. Bound workspace
   - Bind a local folder to one Overleaf project.
   - Keep local metadata for ignore rules, staged paths, merge base, and unresolved conflicts.

3. Sync engine
   - Build local/remote state.
   - Diff it.
   - Push, pull, and merge safely.

4. Repo bridge
   - Add GitHub-oriented commands for a Git repository.
   - Keep Git branch/worktree policy separate from Overleaf sync policy.

5. Remote inspection
   - Tree view.
   - Compile artifacts.
   - PDF download.

## Current Code Boundaries

- [`overleaf_sync/browser_login.py`](/Users/sunyukun/Documents/overleaf-sync/overleaf_sync/browser_login.py)
  - Browser-based login only.

- [`overleaf_sync/local_state.py`](/Users/sunyukun/Documents/overleaf-sync/overleaf_sync/local_state.py)
  - Owns `.ovs-stage.json`, `.ovs-conflicts.json`, `.ovs-conflicts/`, and `.ovs-base/`.
  - This is the persistent workspace state layer.

- [`overleaf_sync/sync_engine.py`](/Users/sunyukun/Documents/overleaf-sync/overleaf_sync/sync_engine.py)
  - Owns file discovery, sync planning, destructive warnings, three-way merge behavior, staged pushes, and pull conflict handling.
  - This is the core sync/merge orchestration layer.

- [`overleaf_sync/git_bridge.py`](/Users/sunyukun/Documents/overleaf-sync/overleaf_sync/git_bridge.py)
  - Owns repository binding config, Git status parsing, branch policy, and Git command execution.
  - This is the Git/GitHub policy layer.

- [`overleaf_sync/cli.py`](/Users/sunyukun/Documents/overleaf-sync/overleaf_sync/cli.py)
  - Now focuses on command wiring plus Overleaf HTTP / realtime client behavior.
  - This remains the transport-facing shell and the final composition layer.

## Refactor Direction

The next clean split should be:

1. `transport.py`
  - Overleaf HTTP / realtime client behavior only.

2. `cli.py`
  - Click command surface only.

The rule is simple:

- transport should not know about Click commands
- sync engine should not know about GitHub policy
- local state should not know about network calls
- CLI should mostly parse arguments and compose services
