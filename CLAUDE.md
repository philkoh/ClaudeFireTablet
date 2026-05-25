# ClaudeFireTablet

Android app targeting Fire HD 10 Plus (11th gen), built without Gradle using a shell script (`build.sh`).

## Standing Orders

- **Always commit, push, and save to memory whenever a new feature is successfully added.** Do this autonomously without being asked.
- Try to commit after successful tests. Avoid committing when the project is in a broken state.
- Default user: Phil Koh <pk14225@gmail.com>
- Remote: git@github.com:philkoh/ClaudeFireTablet.git (uses deploy key at `deploy_key`)

## After Cloning

If this repo was freshly cloned, run `./restore-memories.sh` to restore Claude Code memory files to the correct location. Then regenerate the deploy key if needed and update the git SSH config:

```bash
git config core.sshCommand "ssh -i $(pwd)/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"
```

## Build

```bash
./build.sh
```

## Project Structure

- `src/` — Java source files
- `res/` — Android resources (layouts, strings)
- `AndroidManifest.xml` — app manifest
- `build.sh` — build script (no Gradle)
- `stream.py` — streaming utility
- `.claude-memory/` — tracked copies of Claude Code memory files
