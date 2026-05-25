#!/bin/bash
# Restore Claude Code memory files from the repo to the correct location.
# Run this after cloning the repo to a new location.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
# Claude Code memory path is based on the absolute path of the project, with slashes replaced by dashes
MEMORY_PATH="$HOME/.claude/projects/$(echo "$REPO_DIR" | sed 's|^/||;s|/|-|g')/memory"

mkdir -p "$MEMORY_PATH"
cp "$REPO_DIR/.claude-memory/"*.md "$MEMORY_PATH/"
echo "Memory files restored to $MEMORY_PATH"
