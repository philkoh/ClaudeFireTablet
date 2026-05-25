---
name: feedback-auto-commit
description: "Standing order — always commit, push to GitHub, and save to memory after each successful feature; avoid committing broken state"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 0128266c-c532-425f-a6ed-d8b6e53fe567
---

Always commit, push, and save to memory whenever a new feature is successfully added. Do this autonomously without being asked.

**Why:** The user may delete the local directory at any time and clone from GitHub to continue. Every successful feature must be preserved immediately. Also keeps `.claude-memory/` in sync so memories survive a fresh clone.

**How to apply:** After confirming a feature works (tests pass, build succeeds), stage changes, commit with a descriptive message, push to origin, and update `.claude-memory/` with any new memory files. Try to commit after successful tests. Avoid committing when in a broken state. Related: [[project-github-repo]]
