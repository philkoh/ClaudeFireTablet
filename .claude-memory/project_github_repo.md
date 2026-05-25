---
name: project-github-repo
description: "GitHub repo is philkoh/ClaudeFireTablet, uses deploy key at deploy_key in project root, SSH push via core.sshCommand"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0128266c-c532-425f-a6ed-d8b6e53fe567
---

GitHub repo: philkoh/ClaudeFireTablet (public)
Remote: git@github.com:philkoh/ClaudeFireTablet.git
Deploy key: `deploy_key` / `deploy_key.pub` in project root (ed25519, write access)
Git SSH config: `core.sshCommand = "ssh -i <repo>/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"`

**Why:** User needs to be able to delete local dir and resume from a clone. Deploy key provides repo-scoped access without needing the full GITHUB_TOKEN.

**How to apply:** After cloning, run `restore-memories.sh` and reconfigure `core.sshCommand` pointing to the deploy key. The deploy key files are in `.gitignore` — they must be regenerated or copied manually after a fresh clone. Related: [[feedback-auto-commit]]
