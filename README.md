# scan-notes

Turns photographed handwritten notes into Markdown, using Claude's vision through
the Claude Code CLI so the work draws on a Pro subscription rather than metered
API credits.

Manual trigger. Run it when you're at a machine; there is no background watcher.

```
My Drive/Scanned_20260811-2108.jpg
        -> My Drive/02_Areas/Personal/Reflection AI automated/2026-07-01_relaxed-productivity-ai-prompts.md
        -> My Drive/02_Areas/Personal/Reflection AI automated/2026-07-01_relaxed-productivity-ai-prompts.jpg
```

The original stays untouched in the Drive root. Clear it out by hand whenever you
like — the archive no longer depends on it.

## Setup on a new machine

Google Drive for Desktop mounts `My Drive` at a different path on every machine,
so that path lives in `config.json`, which is gitignored. Only the example is
committed.

1. Install [Google Drive for Desktop](https://www.google.com/drive/download/) and
   let `My Drive` finish syncing.
2. Install [Claude Code](https://claude.com/claude-code) and sign in with your
   Pro account (`claude` once, interactively, is enough).
3. Clone this repo, then:

```bash
pip install pillow pillow-heif
cp config.example.json config.json
```

4. Edit `config.json`:

| key | meaning |
|---|---|
| `drive_root` | This machine's `My Drive` path. Windows: `"H:/My Drive"`. macOS: `"/Users/you/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive"` |
| `claude_bin` | Full path to the `claude` executable. Leave `""` if it's on your PATH. |

`claude_bin` exists because the Windows installer puts `claude` in
`~/.local/bin`, which is often not on PATH in a non-interactive shell. If
`python notes.py` reports it can't find the CLI, set this.

Running with no `config.json` prints these instructions and exits cleanly.

## Use

```bash
python notes.py
```

| flag | effect |
|---|---|
| `--dry-run` | List what would be processed. Calls nothing, writes nothing. |
| `--limit N` | Process at most N images. Use it to work through a backlog without spending your weekly rate limit in one go. |
| `--force` | Reprocess images already recorded as done. |
| `--model` | Override the model, e.g. `--model opus`. |

Roughly 20 seconds and ~8k tokens per page.

## Note format

Content first, metadata at the bottom.

```markdown
# Relaxed productivity — fewer, better AI prompts

The transcription, preserving the structure of the page.

- unreadable words marked like [?Vorlesung]

---
tags: journal, productivity, ai-prompts
date_on_page: 2026-07
scan: 2026-07-01_relaxed-productivity-ai-prompts.jpg
original: Scanned_20260811-2108.jpg
```

`keyword:` appears only when the top of a page carries a deliberate one-word
label. It's the hook for a later categorisation pass.

The metadata sits at the *bottom*, which means it is not YAML frontmatter and
Obsidian will not read it as note properties. That's deliberate — the goal was
Ctrl-F searchability. It only matters if a note app enters the picture later.

## How the date is decided

The date in the filename comes from the page itself, falling back to the scan:

1. A full date written on the page → that date.
2. A partial date (month and year only) → the first of that month.
   `date_on_page:` still records the partial truth, e.g. `2026-07`.
3. No date on the page → the scan date from the original filename.
4. An extracted date that fails validation → the scan date, and the reason is
   recorded in the manifest and printed in the run summary.

Reading conventions, all in `build_prompt()` in [notes.py](notes.py):

- Slashed two-number form, `07/26` → **month/year** → `2026-07`
- Dotted form, `26.07.` → **day.month** → `2026-07-26`
- Missing year → taken from the scan date, rolled back a year if that would
  date the note after its own scan.

Validation rejects any date later than the scan, or more than five years before
it. A model returning a date is not the same as the date being real.

If `date_on_page:` is present in a note, the filename came from the page. If it
is absent, the filename came from the scan — and `original:` is right there, so
either way it's derivable after the fact.

Collisions get `-2`, `-3`. Expect these: several pages written in the same month
all resolve to the first of it.

## Running from two machines

Only code travels through git. The two things that must stay in sync between
machines travel through Drive:

- **`_manifest.json`**, in the output folder — a run log keyed by filename and
  content hash, holding what was processed, when, what the date decision was, and
  what it cost.
- **The notes themselves.** The authoritative answer to "has this page been
  processed" is whether any `.md` in the output folder carries its filename on an
  `original:` line.

That second check is the one that matters. A single JSON file written by two
machines is exactly what Drive resolves by making a conflicted copy, silently
dropping one side's entries. Reading the output folder can't be lost that way, so
the manifest is a cache and a log, never the source of truth. It's also re-read
and merged immediately before every write, and written after every page, so an
interrupted run keeps its progress.

## Swapping the model provider

Everything provider-specific is inside `transcribe()`. It takes an image path and
returns a dict. Nothing else in the script knows Claude exists.

Two flags in there are load-bearing:

- `--safe-mode`, `--system-prompt` and `--tools Read` strip Claude Code's default
  context down to just this task. Measured: ~228k tokens per page without them,
  ~8k with. Don't remove them casually.
- **Never add `--bare`.** Its auth is strictly `ANTHROPIC_API_KEY` or
  `apiKeyHelper` — OAuth and keychain are never read — so it silently bypasses
  the Pro subscription and bills API credits.

For the same reason the script warns, but continues, if `ANTHROPIC_API_KEY` is
set in the environment.

## Deliberate choices

- **One image, one note.** Multi-page notes become separate files.
- **No translation.** Pages are transcribed in the language written. Slugs and
  tags are always English, so one vocabulary covers the whole archive.
- **Tags are fed back.** Existing tags from the output folder are passed to the
  model with instructions to reuse them, so the corpus clusters instead of
  drifting between `ai-prompting` and `ai-prompts`.
- **Filenames that don't match `Scanned_YYYYMMDD-HHMM` are skipped**, not
  date-guessed, and reported in the summary.
- **The archived `.jpg` is the normalised image** — EXIF-rotated, HEIC converted.
  The pristine original stays in the Drive root.
- **Failures are per-page.** One bad image is logged and skipped; the run
  continues and the summary shows what happened.

Because the image is kept beside the note, the whole archive can be
re-transcribed against a better model later. Delete the manifest, or use
`--force`, and run it again.
