# scan-notes

Turns photographed handwritten notes into Markdown, using Claude's vision through
the Claude Code CLI so the work draws on a Pro subscription rather than metered
API credits.

Manual trigger. Run it when you're at a machine; there is no background watcher.

[WORKFLOW.md](WORKFLOW.md) draws the whole pipeline, if you'd rather see it than
read it.

```
My Drive/Scanned_20260811-2108.jpg
        -> .../Reflection AI automated/Markdown/2026-07-01_relaxed-productivity-ai-prompts.md
        -> .../Reflection AI automated/JPEG/2026-07-01_relaxed-productivity-ai-prompts.jpg
```

The note (`.md`) always goes in a `Markdown/` subfolder and its page image(s) in
a `JPEG/` subfolder, both inside `Reflection AI automated/` — kept apart so
skimming one kind of file doesn't mean scrolling past the other.

A PDF works the same way, except that it holds a stack of pages and comes out as
one note per reflection rather than one note per file:

```
My Drive/Scanned_20260819-1500.pdf        (9 pages, 5 reflections)
        -> .../Markdown/2026-07-26_relaxed-productivity.md       (pages 1-3)
        -> .../JPEG/2026-07-26_relaxed-productivity.jpg
        -> .../JPEG/2026-07-26_relaxed-productivity_p2.jpg
        -> .../JPEG/2026-07-26_relaxed-productivity_p3.jpg
        -> .../Markdown/2026-08-02_reading-notes.md              (page 4)
        ...
```

The original stays untouched in the Drive root. Clear it out by hand whenever you
like — the archive no longer depends on it.

**Scan as PDF, always** — one file per scanning session, however many pages are
in it. A one-page PDF produces exactly what a JPEG does, and scanning a batch as
one file is what makes the date carry-forward below possible. Images still work
and are still tested; there is just no reason to reach for them.

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
pip install pillow pillow-heif pymupdf
cp config.example.json config.json
```

`pymupdf` is only needed to read PDFs. Without it, images still work and PDFs are
reported as skipped with the install line as the reason.

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
| `--limit N` | Process at most N **pages**. Use it to work through a backlog without spending your weekly rate limit in one go. It will stop part way through a PDF; the next run picks up at the first page no note covers. |
| `--force` | Reprocess sources already recorded as done. |
| `--model` | Override the model, e.g. `--model opus`. |

Roughly 20 seconds and ~8k tokens per page, PDF pages included: every page is
rasterised and sent on its own, so a 9-page PDF costs nine pages, not more.

## Note format

Content first, metadata at the bottom.

```markdown
# Relaxed productivity — fewer, better AI prompts

The transcription, preserving the structure of the page.

- unreadable words marked like [?Vorlesung]

---
tags: journal, productivity, ai-prompts
date_on_page: 2026-07
scan: 2026-07-01_relaxed-productivity.jpg, 2026-07-01_relaxed-productivity_p2.jpg
pages: 2-3 of 9
original: Scanned_20260819-1500.pdf
```

`scan:` names the image files, not their path — they sit in the sibling `JPEG/`
folder next to this note's `Markdown/` folder.

`scan:` lists one image per page of the reflection, so a note spanning two pages
carries two.

`pages:` appears only when the source held more than one page. It is also what
lets a run interrupted half way through a PDF resume in the right place.

`date_inferred:` takes the place of `date_on_page:` when the reflection carried
no date of its own and one was taken from its neighbours — see below.

`keyword:` appears only when the top of a page carries a deliberate one-word
label. It's the hook for a later categorisation pass.

The metadata sits at the *bottom*, which means it is not YAML frontmatter and
Obsidian will not read it as note properties. That's deliberate — the goal was
Ctrl-F searchability. It only matters if a note app enters the picture later.

## Several reflections in one scan

Scan a stack of pages as one PDF and each reflection still comes out as its own
note. Where one ends and the next begins is read off the pages themselves:

- **A page that carries on the page before it joins that note.** The model is
  shown the tail of the previous page and answers whether this one picks the
  thought back up — mid-sentence, mid-list, or plainly the same train of
  thought. Unsure means no.
- **A page that starts something new starts a new note.** This is the default.
- **A horizontal line drawn across a page does not split it.** Both sides stay
  in one note and the line is transcribed as `***` — *unless* a new date follows
  it. A date always starts a new reflection, so such a page becomes two notes.

Pages inside one note are joined with a blank line, except where a page resumes
mid-sentence, which is joined with a space.

## How the date is decided

The date in the filename comes from the page itself, falling back to the scan:

1. A full date written on the page → that date.
2. A partial date (month and year only) → the first of that month.
   `date_on_page:` still records the partial truth, e.g. `2026-07`.
3. No date on the page, but an earlier page of the same scan has one → **that
   date**. Pages within one scan are in chronological order, so a page you
   forgot to date belongs to the last date written before it, not to the day you
   happened to scan it. The note records
   `date_inferred: 2026-07-26 (carried from page 2)`.
4. No date before it, but one after it → that date, recorded as
   `(inferred from page N)`. The same chronology puts an undated opening page at
   or before the first date in the file, which beats the scan date.
5. No date anywhere in the source → the scan date from the original filename.
   This is the ordinary case for a single-page upload.
6. An extracted date that fails validation → treated as no date at all, so rules
   3-5 apply, and the reason is recorded in the manifest and printed in the run
   summary.

Carrying a date forward never crosses files: a scan is only trusted to be in
order within itself. Page dates that run *backwards* inside one file are used as
written and flagged in the summary, since that is the assumption breaking.

Reading conventions, all in `build_prompt()` in [notes.py](notes.py):

- Slashed two-number form, `07/26` → **month/year** → `2026-07`
- Dotted form, `26.07.` → **day.month** → `2026-07-26`
- Missing year → taken from the year on the page before it in the same scan,
  or from the scan date, rolled back a year if that would date the note after
  its own scan.

Validation rejects any date later than the scan, or more than five years before
it. A model returning a date is not the same as the date being real.

`date_on_page:` means the filename came from the page. `date_inferred:` means it
came from a neighbouring page, and says which one. Neither means it came from the
scan. `original:` is there in all three cases, so the decision stays derivable
after the fact.

Collisions get `-2`, `-3`. Expect these: several pages written in the same month
all resolve to the first of it.

## Running from two machines

Only code travels through git. The two things that must stay in sync between
machines travel through Drive:

- **`_manifest.json`**, directly in the output folder (not inside `Markdown/` or
  `JPEG/`) — a run log keyed by filename and content hash, holding what was
  processed, when, what the date decision was, and what it cost.
- **The notes themselves.** The authoritative answer to "has this page been
  processed" is whether any `.md` in `Markdown/` carries its filename on an
  `original:` line — and for a multi-page source, whether some note's `pages:`
  line covers that page number. A PDF stopped part way through by `--limit` or
  Ctrl-C resumes at its first uncovered page, and reads the date it should carry
  forward back out of the notes the earlier run wrote, so a split run dates its
  notes exactly as an uninterrupted one would. The only cost of stopping mid
  file is that a reflection straddling the break comes out as two notes.

If an older run left notes and images loose directly in the output folder
(from before `Markdown/`/`JPEG/` existed), the next run moves them into place
first, so this check still finds them.

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

- **One reflection, one note.** A single image is one page; a PDF is a stack of
  them, cut into reflections by what is written on them. Each note keeps an
  image of every page it covers.
- **Splitting is biased towards separate notes.** Two reflections welded into
  one file is the harder mistake to notice months later; one reflection split
  across two files is obvious the moment you read them.
- **No translation.** Pages are transcribed in the language written. Slugs and
  tags are always English, so one vocabulary covers the whole archive.
- **Tags are fed back.** Existing tags from the output folder are passed to the
  model with instructions to reuse them, so the corpus clusters instead of
  drifting between `ai-prompting` and `ai-prompts`.
- **Filenames that don't match `Scanned_YYYYMMDD-HHMM` are skipped**, not
  date-guessed, and reported in the summary.
- **PDF pages are rendered, not pulled out.** Rendering at ~2200px on the long
  edge stays correct when a scanner splits one page into several images, and the
  model sees exactly what gets archived beside the note.
- **The archived `.jpg` is the normalised image** — EXIF-rotated, HEIC converted.
  The pristine original stays in the Drive root.
- **Failures are per-page.** One bad page is logged and skipped; the rest of its
  file, and the run, carry on, and the summary shows what happened.

Because the image is kept beside the note, the whole archive can be
re-transcribed against a better model later. Delete the manifest, or use
`--force`, and run it again.
