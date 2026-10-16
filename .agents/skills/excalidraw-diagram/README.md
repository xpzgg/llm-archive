# Excalidraw Diagram Skill

Creates editable, self-contained `.excalidraw` JSON files. Default output is one diagram file, suitable for copying to an internal environment and opening/importing with the Obsidian Excalidraw plugin.

PNG rendering is optional and runs only when explicitly requested. It uses Playwright with headless Chromium and a local `file://` template; no HTTP server or listening port is started. The template loads Excalidraw from esm.sh and therefore needs network access during rendering.

For optional rendering setup, run from this skill's `references` directory:

```bash
uv sync
uv run playwright install chromium
uv run python render_excalidraw.py /absolute/path/diagram.excalidraw
```

Do not install rendering dependencies for ordinary diagram-file generation. See `SKILL.md` for drawing, structural validation, and optional image inspection guidance.
