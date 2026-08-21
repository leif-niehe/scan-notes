# scan-notes

Scanned handwritten reflections → Markdown, one note per reflection, through the
Claude Code CLI so the work draws on a Pro subscription. All the code is in
[notes.py](notes.py).

## Keep the map in sync

[WORKFLOW.md](WORKFLOW.md) draws the whole pipeline in Mermaid. **It is part of
any change to the workflow, not a follow-up to it** — when behaviour changes,
update the affected diagram in the same commit, and check that the prose in
[README.md](README.md) still agrees. Its final section lists which diagram
answers to which kind of change. A stale diagram is worse than no diagram: if one
can't be made correct, delete it.

## Load-bearing details, easy to break by accident

- **Never add `--bare`** to the CLI call in `transcribe`. Its auth is strictly
  `ANTHROPIC_API_KEY` or `apiKeyHelper`, so it silently bypasses the Pro
  subscription and bills API credits.
- `--safe-mode`, `--system-prompt` and `--tools Read` are what keep a page at
  ~8k tokens instead of ~228k. Don't drop them casually.
- The **output folder**, not `_manifest.json`, decides what still needs doing.
  The manifest is a log and a cache; a Drive sync conflict can silently drop half
  of it, so it must never become the source of truth.
- Dates carry forward **within one scan only**. Pages of one file are assumed to
  be in chronological order; separate uploads are not.
- Everything provider-specific lives in `transcribe`. Keep it that way, so
  swapping models means rewriting one function.

## Testing without spending tokens

`transcribe` is the only thing that calls out. Stub it, point `load_config` at a
temp folder, and `main()` runs end to end offline — that is how the grouping,
date and resume logic should be exercised.
