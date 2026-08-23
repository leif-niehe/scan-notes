#!/usr/bin/env python3
"""Turn scanned handwritten notes into Markdown files.

Manual trigger: run it when you're at a machine that syncs Drive.
Reads  <drive_root>/Scanned_*.{jpg,png,heic,pdf}
Writes <drive_root>/02_Areas/Personal/Reflection AI automated/

A single image is one page. A PDF is a stack of pages that may hold several
reflections; each reflection becomes its own note.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent
CONFIG = REPO / "config.json"
CONFIG_EXAMPLE = REPO / "config.example.json"

OUTPUT_SUBPATH = "02_Areas/Personal/Reflection AI automated"
MD_SUBDIR = "Markdown"
IMG_SUBDIR = "JPEG"
MANIFEST_NAME = "_manifest.json"

SCAN_RE = re.compile(r"^Scanned_(\d{8})-(\d{4})", re.IGNORECASE)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
PDF_EXTS = {".pdf"}
SOURCE_EXTS = IMAGE_EXTS | PDF_EXTS

# An extracted page date this much older than the scan is treated as a misread.
MAX_BACKDATE_DAYS = 5 * 365
CALL_TIMEOUT_S = 300

# PDF pages are rasterised to this long edge. The model downsamples below this
# anyway; the headroom is for re-transcribing against a better model later, and
# for Leif reading the archived page himself rather than the transcription.
PDF_LONG_EDGE_PX = 3400
PDF_JPEG_QUALITY = 95

CLAUDE_FALLBACKS = [
    Path.home() / ".local" / "bin" / "claude.exe",
    Path.home() / ".local" / "bin" / "claude",
    Path.home() / ".claude" / "local" / "claude.exe",
    Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
]


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

SETUP_HELP = """\
No config.json found.

This repo needs to know where Google Drive is mounted on THIS machine, which
differs per machine. That path is deliberately not committed.

  1. Copy config.example.json to config.json
  2. Set "drive_root" to this machine's My Drive path
       Windows : "H:/My Drive"
       macOS   : "/Users/you/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive"
  3. Run again

config.json is gitignored, so this is a one-time step per machine.
"""


def load_config() -> dict:
    if not CONFIG.exists():
        print(SETUP_HELP)
        sys.exit(0)
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"config.json is not valid JSON: {e}")

    root = cfg.get("drive_root", "").strip()
    if not root:
        sys.exit('config.json is missing "drive_root".')
    cfg["drive_root"] = Path(root).expanduser()
    if not cfg["drive_root"].is_dir():
        sys.exit(f"drive_root does not exist: {cfg['drive_root']}\nIs Google Drive running?")
    return cfg


def find_claude(cfg: dict) -> str:
    explicit = cfg.get("claude_bin", "").strip()
    if explicit:
        if not Path(explicit).exists():
            sys.exit(f'claude_bin in config.json does not exist: {explicit}')
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    for cand in CLAUDE_FALLBACKS:
        if cand and cand.exists():
            return str(cand)
    sys.exit(
        "Could not find the `claude` CLI.\n"
        'Add its full path to config.json as "claude_bin".'
    )


def check_billing() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "!! ANTHROPIC_API_KEY is set in this environment.\n"
            "!! Claude Code will bill metered API credits instead of your Pro\n"
            "!! subscription. Unset it and re-run if that is not what you want.\n"
        )


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def scan_date_from_name(name: str) -> dt.date | None:
    """Scanned_20260811-2108.jpg -> 2026-08-11. None if the name doesn't match."""
    m = SCAN_RE.match(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def parse_page_date(iso: str | None, scan: dt.date) -> tuple[dt.date | None, str | None, str]:
    """Validate the model's ISO reading of the date written on the page.

    Returns (date_for_filename, text_for_date_on_page_line, note).
    A partial "YYYY-MM" becomes the first of that month, per the naming rule,
    while date_on_page keeps the partial truth.
    """
    if not iso:
        return None, None, ""
    iso = iso.strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
        try:
            d = dt.date.fromisoformat(iso)
        except ValueError:
            return None, None, f"unparseable page date {iso!r}"
        partial = False
    elif re.fullmatch(r"\d{4}-\d{2}", iso):
        try:
            d = dt.date(int(iso[:4]), int(iso[5:7]), 1)
        except ValueError:
            return None, None, f"unparseable page date {iso!r}"
        partial = True
    else:
        return None, None, f"unrecognised date format {iso!r}"

    # A note cannot be written after it was scanned.
    if d > scan:
        return None, None, f"page date {iso} is after the scan date {scan}"
    if (scan - d).days > MAX_BACKDATE_DAYS:
        return None, None, f"page date {iso} is implausibly long before the scan {scan}"

    return d, (iso if partial else d.isoformat()), ""


# --------------------------------------------------------------------------
# slugs and filenames
# --------------------------------------------------------------------------

def slugify(raw: str, max_words: int = 6) -> str:
    s = unicodedata.normalize("NFKD", raw or "")
    s = s.replace("ß", "ss")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    words = [w for w in s.split("-") if w][:max_words]
    return "-".join(words) or "untitled"


def image_names_for(stem: str, count: int) -> list[str]:
    """First page keeps the bare stem; later pages of the same note get _p2, _p3."""
    return [f"{stem}.jpg" if i == 0 else f"{stem}_p{i + 1}.jpg" for i in range(count)]


def claim_names(md_dir: Path, img_dir: Path, date: dt.date, slug: str,
                image_count: int) -> tuple[Path, list[Path]]:
    """Find a free (note file, image files) filename set, adding -2, -3 on
    collision. The note goes in md_dir, its page images in img_dir."""
    base = f"{date.isoformat()}_{slug}"
    n = 1
    while True:
        stem = base if n == 1 else f"{base}-{n}"
        md = md_dir / f"{stem}.md"
        imgs = [img_dir / name for name in image_names_for(stem, image_count)]
        if not md.exists() and not any(p.exists() for p in imgs):
            return md, imgs
        n += 1


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

def file_key(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return f"{path.name}:{h.hexdigest()[:16]}"


def load_manifest(out_dir: Path) -> dict:
    p = out_dir / MANIFEST_NAME
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        print(f"   (manifest at {p} unreadable, treating as empty)")
        return {}


def save_manifest(out_dir: Path, new_entries: dict) -> None:
    """Re-read and merge immediately before writing.

    Two machines share this file through Drive sync. Merging on every write
    keeps a stale in-memory copy from clobbering the other machine's entries.
    """
    p = out_dir / MANIFEST_NAME
    merged = load_manifest(out_dir)
    merged.update(new_entries)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


ORIGINAL_RE = re.compile(r"^original:\s*(.+?)\s*$", re.MULTILINE)
PAGES_RE = re.compile(r"^pages:\s*(\d+)(?:\s*-\s*(\d+))?\s+of\s+(\d+)\s*$", re.MULTILINE)


def pages_from_manifest(manifest: dict, key: str) -> set[int]:
    """Pages the run log says are done, so a note you deleted on purpose stays
    deleted instead of coming back on the next run. Entries written before
    multi-page sources existed record a single page."""
    entry = manifest.get(key)
    if not entry:
        return set()
    return set(entry.get("pages_done") or [1])


def pages_already_written(md_dir: Path) -> dict[str, set[int]]:
    """Source filename -> page numbers already turned into notes.

    The notes folder, not the manifest, is the authoritative answer to "has
    this been processed". It cannot be lost to a Drive sync conflict. Notes
    from a multi-page source carry a `pages:` line, so a run cut short by
    --limit resumes at the first page no note covers.
    """
    seen: dict[str, set[int]] = {}
    for md in md_dir.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        m = ORIGINAL_RE.search(text)
        if not m:
            continue
        covered = seen.setdefault(m.group(1), set())
        p = PAGES_RE.search(text)
        if p:
            first = int(p.group(1))
            last = int(p.group(2) or p.group(1))
            covered.update(range(first, last + 1))
        else:
            covered.add(1)
    return seen


DATE_ON_PAGE_RE = re.compile(r"^date_on_page:\s*(\S+)\s*$", re.MULTILINE)


def iso_to_date(iso: str) -> dt.date | None:
    """"YYYY-MM-DD", or "YYYY-MM" as the first of that month. None otherwise."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}", iso):
            return dt.date(int(iso[:4]), int(iso[5:7]), 1)
        return dt.date.fromisoformat(iso)
    except ValueError:
        return None


def carry_seed(md_dir: Path, original: str) -> tuple[dt.date, int] | None:
    """The last date written on an already-processed page of this source.

    A run cut short by --limit must date its remaining pages the same way an
    uninterrupted run would, so resuming picks the carry-forward back up
    instead of starting blind.
    """
    best: tuple[dt.date, int, int] | None = None   # date, its page, note's last page
    for md in md_dir.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        m = ORIGINAL_RE.search(text)
        if not m or m.group(1) != original:
            continue
        d = DATE_ON_PAGE_RE.search(text)
        if not d:
            continue
        date = iso_to_date(d.group(1))
        if not date:
            continue
        pages = PAGES_RE.search(text)
        first = int(pages.group(1)) if pages else 1
        last = int(pages.group(2) or pages.group(1)) if pages else 1
        if best is None or last > best[2]:
            # The date was written where the note starts, but a later-ending
            # note is the more recent one to carry forward from.
            best = (date, first, last)
    return (best[0], best[1]) if best else None


# --------------------------------------------------------------------------
# page preparation
# --------------------------------------------------------------------------

def wait_until_stable(path: Path, tries: int = 4, pause: float = 2.0) -> bool:
    """Guard against reading a file the sync client is still writing."""
    last = -1
    for _ in range(tries):
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last and size > 0:
            return True
        last = size
        time.sleep(pause)
    return False


def prepare_image(src: Path, workdir: Path) -> tuple[Path, str | None]:
    """Normalise orientation and convert HEIC. Returns (path_to_use, note).

    Returns the untouched source when no work is needed, so ordinary JPEGs are
    never re-encoded.
    """
    ext = src.suffix.lower()
    is_heic = ext in {".heic", ".heif"}

    try:
        from PIL import Image, ImageOps
    except ImportError:
        if is_heic:
            return src, "HEIC needs Pillow: pip install pillow pillow-heif"
        return src, None

    if is_heic:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            return src, "HEIC needs pillow-heif: pip install pillow-heif"

    try:
        with Image.open(src) as im:
            orientation = (im.getexif() or {}).get(0x0112, 1)
            if not is_heic and orientation in (1, 0):
                return src, None
            fixed = ImageOps.exif_transpose(im)
            if fixed.mode not in ("RGB", "L"):
                fixed = fixed.convert("RGB")
            out = workdir / (src.stem + ".jpg")
            fixed.save(out, "JPEG", quality=92)
        return out, ("converted from HEIC" if is_heic else "rotated via EXIF")
    except Exception as e:  # noqa: BLE001 - a bad image must not kill the run
        return src, f"image preprocessing failed ({e}), using original"


def _pymupdf():
    try:
        import pymupdf  # PyMuPDF >= 1.24 exposes this name
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # older PyMuPDF
        return fitz
    except ImportError:
        raise RuntimeError("PDF input needs PyMuPDF: pip install pymupdf") from None


def source_page_count(path: Path) -> int:
    """Pages in a source file. Images are one page; anything unreadable is one."""
    if path.suffix.lower() not in PDF_EXTS:
        return 1
    try:
        with _pymupdf().open(path) as doc:
            return max(1, doc.page_count)
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001 - a broken PDF is reported when it is rendered
        return 1


def render_pdf_pages(src: Path, workdir: Path,
                     wanted: set[int] | None = None) -> list[tuple[int, Path]]:
    """Rasterise PDF pages to JPEG. Returns [(page_number, path)].

    Rendering rather than pulling the embedded image out keeps this correct for
    pages the scanner split into several images, and gives the model exactly
    what gets archived beside the note.
    """
    pm = _pymupdf()
    out: list[tuple[int, Path]] = []
    with pm.open(src) as doc:
        for i in range(doc.page_count):
            n = i + 1
            if wanted is not None and n not in wanted:
                continue
            page = doc.load_page(i)
            long_edge = max(page.rect.width, page.rect.height) or 1
            zoom = min(max(PDF_LONG_EDGE_PX / long_edge, 0.5), 6.0)
            pix = page.get_pixmap(matrix=pm.Matrix(zoom, zoom))
            dest = workdir / f"{slugify(src.stem, 12)}_p{n:03d}.jpg"
            try:
                pix.pil_save(dest, format="JPEG", quality=PDF_JPEG_QUALITY)
            except Exception:  # noqa: BLE001 - Pillow missing or unhappy
                dest = dest.with_suffix(".png")
                pix.save(dest)
            out.append((n, dest))
    return out


def prepare_pages(src: Path, workdir: Path,
                  wanted: set[int] | None = None) -> tuple[list[tuple[int, Path]], str | None]:
    """Return ([(page_number, image_path)], note) for any supported source."""
    if src.suffix.lower() in PDF_EXTS:
        pages = render_pdf_pages(src, workdir, wanted)
        if not pages:
            raise RuntimeError("PDF produced no pages")
        return pages, f"rendered {len(pages)} page(s) from PDF"
    img, note = prepare_image(src, workdir)
    return [(1, img)], note


# --------------------------------------------------------------------------
# the model call - the only provider-specific code in this file
# --------------------------------------------------------------------------

SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "continues_previous": {"type": "boolean"},
        "title": {"type": "string"},
        "markdown": {"type": "string"},
        "slug": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "keyword": {"type": ["string", "null"]},
        "date_raw": {"type": ["string", "null"]},
        "date_iso": {"type": ["string", "null"]},
    },
    "required": ["continues_previous", "title", "markdown", "slug", "tags",
                 "keyword", "date_raw", "date_iso"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {"segments": {"type": "array", "items": SEGMENT_SCHEMA}},
    "required": ["segments"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You transcribe photographed handwritten notes. Read the image with the Read tool, "
    "then return the structured result and nothing else. "
    "Transcribe in the language written; never translate. "
    "Preserve the structure of the page: headings, bullets, indentation, emphasis. "
    "Mark words you cannot read confidently as [?bestguess]. "
    "Never summarise, interpret, comment on, or add anything that is not on the page."
)


def build_prompt(image: Path, scan_date: dt.date, known_tags: list[str],
                 ctx: dict | None) -> str:
    """ctx places this page inside a multi-page scan, or is None for a lone image.

    Everything ctx adds is about joining pages up. The transcription
    instructions themselves are identical either way.
    """
    vocab = (
        "Tags already used in this archive - reuse these wherever one fits, and only "
        "invent a new tag when none does:\n  " + ", ".join(known_tags) + "\n\n"
        if known_tags else ""
    )

    where = ""
    previous = ""
    continues_rule = (
        "continues_previous\n"
        "  Always false. This page stands alone.\n\n"
    )

    if ctx:
        where = f"It is page {ctx['page']} of {ctx['total']} in that scan.\n"
        if ctx.get("prev_page"):
            tail = ctx.get("prev_tail") or ""
            previous = (
                f"Page {ctx['prev_page']}, immediately before this one, ended as follows.\n"
                f"  its title : {ctx.get('prev_title') or '(none)'}\n"
                f"  its date  : {ctx.get('prev_date_raw') or '(none written)'}\n"
                "  its last lines:\n"
                + "\n".join("    " + ln for ln in tail.splitlines())
                + "\n\n"
            )
            continues_rule = (
                "continues_previous\n"
                "  True when this entry carries on the entry from the page above - it\n"
                "  picks up mid-sentence or mid-list, or plainly continues the same\n"
                "  thought under the same heading. False when it starts something new.\n"
                "  An entry that carries its own new date is never a continuation.\n"
                "  When genuinely unsure, answer false.\n\n"
            )
        else:
            continues_rule = (
                "continues_previous\n"
                "  Always false: no page of this scan comes before this one.\n\n"
            )

    year_hint = ""
    if ctx and ctx.get("prev_year"):
        year_hint = (
            f"    - The page before this one was written in {ctx['prev_year']}. If this\n"
            f"      entry writes no year, that is the year to use.\n"
        )

    return f"""Transcribe the handwritten page at {image}

This page was scanned on {scan_date.isoformat()}.
{where}
{previous}If the page is blank, or carries no more than a stray mark, page number,
or printed artifact with nothing handwritten to transcribe, return exactly one
segment with markdown, title and slug set to "", tags set to [], and keyword,
date_raw, date_iso set to null. Do not invent content to fill it.

A page usually holds exactly one journal entry, so usually you return
exactly one segment. Return more than one ONLY where a horizontal divider drawn
on the page is followed by a NEW DATE. A divider on its own does not start a new
entry: keep the text on both sides of it in the same segment, and transcribe the
divider itself as *** on a line of its own. When in doubt, do not split.

Return these fields for every segment:

{continues_rule}title
  A short title for the entry. If it has its own heading, use it verbatim.
  If it does not, write a brief descriptive one in the entry's own language.
  For a continuation, repeat the previous page's title.

markdown
  The transcription, WITHOUT repeating the title as a heading. Keep the page's
  structure. Do not translate.

slug
  3-6 words, lowercase, hyphen-separated, ENGLISH, from the entry's content.

tags
  2-5 lowercase ENGLISH topic tags.

{vocab}keyword
  If the very top of the entry carries a deliberate one-word label (a category
  marker, not a heading or a date), return it. Otherwise null.

date_raw
  The date written on the entry, copied exactly as it appears. Null if it
  carries no date.

date_iso
  Your reading of that date, using these conventions:
    - A slashed two-number form like 07/26 means MONTH/YEAR -> "2026-07"
    - A dotted form like 26.07. means DAY.MONTH -> "2026-07-26"
{year_hint}    - If no year is written, take it from the scan date above. A note cannot be
      written after it was scanned, so roll back one year if that would happen.
    - "YYYY-MM-DD" when the day is known, "YYYY-MM" when only month and year
      are, null when the entry carries no date.
  Do not guess a day that is not written. Return null rather than inferring a
  date from surrounding pages - that is filled in afterwards, not by you.
"""


def transcribe(image_path: Path, claude_bin: str, scan_date: dt.date,
               known_tags: list[str], ctx: dict | None = None,
               model: str | None = None) -> dict:
    """Call the model on one page and return the parsed payload.

    Everything provider-specific lives here. Swapping providers means
    rewriting this function and nothing else.

    The flags matter: --safe-mode, --system-prompt and --tools Read strip the
    default Claude Code context down to just this task, which cut measured
    usage from ~228k tokens per page to ~8k. Do NOT add --bare: it forces
    ANTHROPIC_API_KEY auth and bypasses the Pro subscription.
    """
    cmd = [
        claude_bin, "-p", build_prompt(image_path, scan_date, known_tags, ctx),
        "--safe-mode",
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "Read",
        "--allowed-tools", "Read",
        "--add-dir", str(image_path.parent),
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
    ]
    if model:
        cmd += ["--model", model]

    with tempfile.TemporaryDirectory() as neutral_cwd:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=CALL_TIMEOUT_S, cwd=neutral_cwd,
        )

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")

    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude returned non-JSON: {proc.stdout[:300]!r}") from None

    if env.get("is_error") or env.get("subtype") != "success":
        raise RuntimeError(f"claude reported {env.get('subtype')}: {str(env.get('result'))[:300]}")

    payload = env.get("structured_output")
    if payload is None:
        raw = env.get("result")
        if not isinstance(raw, str):
            raise RuntimeError("no structured output in response")
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        payload = json.loads(raw)

    segments = payload.get("segments")
    if isinstance(segments, dict):          # a lone segment, unwrapped
        segments = [segments]
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("no segments in response")

    return {
        "segments": segments,
        "_usage": env.get("usage", {}),
        "_cost_usd": env.get("total_cost_usd"),
    }


# --------------------------------------------------------------------------
# note assembly
# --------------------------------------------------------------------------

BLOCK_START_RE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||\*\*\*|---)")
OPEN_TAIL_RE = re.compile(r"[\w,;:\-\u2013\u2014]$")


def join_markdown(chunks: list[str]) -> str:
    """Stitch a note's pages together.

    A page that resumes mid-sentence is joined with a space; anything else gets
    a blank line, so a continuation never silently welds two paragraphs.
    """
    out = ""
    for chunk in chunks:
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        if not out:
            out = chunk
            continue
        last_line = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
        mid_sentence = (
            OPEN_TAIL_RE.search(last_line.rstrip())
            and not BLOCK_START_RE.match(chunk)
            and chunk[:1].islower()
        )
        out += (" " if mid_sentence else "\n\n") + chunk
    return out


def build_note(note: dict, original_name: str, total_pages: int) -> str:
    """Content first, metadata at the bottom. Not frontmatter, by design."""
    title = (note["title"] or "").strip() or "Untitled"
    parts = [f"# {title}", "", note["body"].strip(), "", "---"]

    if note.get("keyword"):
        parts.append(f"keyword: {note['keyword']}")
    if note.get("tags"):
        parts.append(f"tags: {', '.join(note['tags'])}")
    if note.get("date_on_page"):
        parts.append(f"date_on_page: {note['date_on_page']}")
    elif note.get("date_inferred"):
        parts.append(f"date_inferred: {note['date_inferred']}")
    parts.append(f"scan: {', '.join(note['image_names'])}")
    if total_pages > 1:
        parts.append(f"pages: {page_span(note['pages'])} of {total_pages}")
    parts.append(f"original: {original_name}")
    return "\n".join(parts) + "\n"


def page_span(pages: list[int]) -> str:
    return f"{pages[0]}" if pages[0] == pages[-1] else f"{pages[0]}-{pages[-1]}"


TAGS_RE = re.compile(r"^tags:\s*(.+?)\s*$", re.MULTILINE)


def collect_known_tags(md_dir: Path, limit: int = 40) -> list[str]:
    """Existing vocabulary, most used first, so tags cluster instead of drifting."""
    counts: dict[str, int] = {}
    for md in md_dir.glob("*.md"):
        try:
            m = TAGS_RE.search(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not m:
            continue
        for tag in m.group(1).split(","):
            tag = tag.strip().lower()
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def enforce_date_boundaries(segments: list[dict]) -> None:
    """A segment carrying its own date can never continue a different date
    already established earlier in the file.

    The prompt already asks the model for this, but a misread here silently
    welds two days into one note - the mistake nobody notices until months
    later. So it is enforced here too, in code, not left to the model alone.
    Mutates continues_previous in place; equal or absent dates are untouched.
    """
    last_date: str | None = None
    for seg in segments:
        cur = (seg.get("date_iso") or "").strip() or None
        if cur and last_date and cur != last_date and seg.get("continues_previous"):
            seg["continues_previous"] = False
        if cur:
            last_date = cur


def group_segments(segments: list[dict]) -> list[list[dict]]:
    """One group per reflection. A group spans pages; a page can start a group."""
    groups: list[list[dict]] = []
    for seg in segments:
        if groups and seg.get("continues_previous"):
            groups[-1].append(seg)
        else:
            groups.append([seg])
    return groups


def resolve_dates(groups: list[list[dict]], scan_date: dt.date,
                  seed: tuple[dt.date, int] | None = None) -> list[dict]:
    """Decide each reflection's date, carrying dates forward across the file.

    Pages within one scan are in chronological order, so an undated entry
    belongs to the last date written before it. That inference only exists when
    something else in the same file carries a date; otherwise the scan date
    stands, exactly as it does for a single-page upload.
    """
    resolved: list[dict] = []
    for g in groups:
        page_date = None
        date_on_page = None
        note = ""
        for seg in g:
            d, text, why = parse_page_date(seg.get("date_iso"), scan_date)
            note = note or why
            if d and page_date is None:
                page_date, date_on_page = d, text
        resolved.append({
            "group": g,
            "page_date": page_date,
            "date_on_page": date_on_page,
            "date_note": note,
        })

    # Forwards: an undated entry inherits the last date written before it,
    # including one written on a page an earlier run already turned into a note.
    last: tuple[dt.date, int] | None = seed
    for r in resolved:
        if r["page_date"]:
            last = (r["page_date"], r["group"][0]["_page"])
        elif last:
            r["date"] = last[0]
            r["date_source"] = "carried"
            r["date_inferred"] = f"{last[0].isoformat()} (carried from page {last[1]})"

    # Backwards, for entries standing before the first date in the file:
    # chronological order puts them at or before it, which beats the scan date.
    nxt: tuple[dt.date, int] | None = None
    for r in reversed(resolved):
        if r["page_date"]:
            nxt = (r["page_date"], r["group"][0]["_page"])
        elif not r.get("date") and nxt:
            r["date"] = nxt[0]
            r["date_source"] = "inferred"
            r["date_inferred"] = f"{nxt[0].isoformat()} (inferred from page {nxt[1]})"

    backwards = ""
    prev_written: dt.date | None = None
    for r in resolved:
        if r["page_date"]:
            r["date"] = r["page_date"]
            r["date_source"] = "page"
            if prev_written and r["page_date"] < prev_written:
                backwards = (f"page dates run backwards ({prev_written} then "
                             f"{r['page_date']}); dates were used as written")
            prev_written = r["page_date"]
        elif not r.get("date"):
            r["date"] = scan_date
            r["date_source"] = "scan"
    if backwards:
        resolved[0]["date_note"] = "; ".join(
            x for x in (resolved[0]["date_note"], backwards) if x
        )
    return resolved


def assemble_note(r: dict) -> dict:
    """Fold one group of segments into the fields a note file needs."""
    g = r["group"]
    tags: list[str] = []
    for seg in g:
        for t in seg.get("tags") or []:
            t = t.strip().lower()
            if t and t not in tags:
                tags.append(t)
    keyword = next(((s.get("keyword") or "").strip() for s in g
                    if (s.get("keyword") or "").strip()), "")
    pages: list[int] = []
    images: list[Path] = []
    for seg in g:
        if seg["_page"] not in pages:
            pages.append(seg["_page"])
            images.append(seg["_image"])
    return {
        "title": g[0].get("title") or "",
        "body": join_markdown([s.get("markdown") or "" for s in g]),
        "slug": slugify(g[0].get("slug") or g[0].get("title") or "untitled"),
        "tags": tags[:6],
        "keyword": keyword,
        "pages": pages,
        "sources": images,
        "date": r["date"],
        "date_source": r["date_source"],
        "date_on_page": r.get("date_on_page"),
        "date_inferred": r.get("date_inferred"),
        "date_note": r.get("date_note") or "",
    }


def context_for(segments: list[dict], page_no: int, total: int) -> dict:
    """What page `page_no` needs to know about the page before it."""
    ctx: dict = {"page": page_no, "total": total}
    if not segments:
        return ctx
    prev = segments[-1]
    body = (prev.get("markdown") or "").strip()
    ctx.update({
        "prev_page": prev["_page"],
        "prev_title": prev.get("title"),
        "prev_tail": "\n".join(body.splitlines()[-6:])[-500:],
        "prev_date_raw": prev.get("date_raw"),
    })
    for seg in reversed(segments):
        iso = (seg.get("date_iso") or "").strip()
        if re.match(r"^\d{4}", iso):
            ctx["prev_year"] = iso[:4]
            break
    return ctx


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_queue(md_dir: Path, candidates: list[Path], done: dict[str, set[int]],
                manifest: dict, force: bool) -> tuple[list[dict], list[tuple[str, str]]]:
    """What to process, page by page. Pages already covered by a note are skipped,
    so a run stopped by --limit or Ctrl-C picks up where it left off."""
    queue: list[dict] = []
    skipped: list[tuple[str, str]] = []

    for path in candidates:
        if scan_date_from_name(path.name) is None:
            skipped.append((path.name, "filename has no Scanned_YYYYMMDD-HHMM date"))
            continue
        if not wait_until_stable(path):
            skipped.append((path.name, "still syncing, try again later"))
            continue
        key = file_key(path)
        logged = {} if force else manifest.get(key, {})
        if logged.get("pages_total") and                 len(pages_from_manifest(manifest, key)) >= logged["pages_total"]:
            continue    # finished earlier; no need to open it, PDF reader or not

        try:
            total = source_page_count(path)
        except RuntimeError as e:
            skipped.append((path.name, str(e)))
            continue

        covered = (set() if force else
                   done.get(path.name, set()) | pages_from_manifest(manifest, key))
        todo = [n for n in range(1, total + 1) if n not in covered]
        if not todo:
            continue
        if covered:
            print(f"Resuming {path.name}: {len(todo)} of {total} page(s) left. "
                  "An entry spanning the break will come out as two notes.\n")
        queue.append({"path": path, "key": key, "pages": todo, "total": total,
                      "seed": carry_seed(md_dir, path.name) if covered else None})

    return queue, skipped


def migrate_flat_layout(out_dir: Path, md_dir: Path, img_dir: Path) -> int:
    """Move notes and images an older run left directly in out_dir into the
    Markdown/ and JPEG/ subfolders.

    One-time and safe to run every time: once nothing is left loose in
    out_dir, there is nothing to move. This has to happen before anything
    reads md_dir, or pages already done would look undone and get reprocessed.
    """
    moved = 0
    for p in out_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() == ".md":
            dest = md_dir / p.name
        elif p.suffix.lower() == ".jpg":
            dest = img_dir / p.name
        else:
            continue
        if dest.exists():
            continue
        shutil.move(str(p), str(dest))
        moved += 1
    return moved


def apply_limit(queue: list[dict], limit: int) -> list[dict]:
    """Trim the queue to `limit` pages, cutting a file mid-way if need be."""
    budget = limit
    trimmed: list[dict] = []
    for item in queue:
        if budget <= 0:
            break
        if len(item["pages"]) > budget:
            item = {**item, "pages": item["pages"][:budget]}
        budget -= len(item["pages"])
        trimmed.append(item)
    return trimmed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, metavar="N",
                    help="process at most N pages this run")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be processed, call nothing, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="reprocess sources even if they are already recorded")
    ap.add_argument("--model", help="override the model (e.g. opus, sonnet)")
    args = ap.parse_args()

    cfg = load_config()
    claude_bin = find_claude(cfg)
    check_billing()

    drive_root: Path = cfg["drive_root"]
    out_dir = drive_root / OUTPUT_SUBPATH
    md_dir = out_dir / MD_SUBDIR
    img_dir = out_dir / IMG_SUBDIR
    md_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)
    moved = migrate_flat_layout(out_dir, md_dir, img_dir)
    if moved:
        print(f"Moved {moved} file(s) from the old flat layout into "
              f"{MD_SUBDIR}/ and {IMG_SUBDIR}/.\n")

    candidates = sorted(
        p for p in drive_root.glob("Scanned_*")
        if p.is_file() and p.suffix.lower() in SOURCE_EXTS
    )
    if not candidates:
        print(f"No Scanned_* images or PDFs in {drive_root}")
        return 0

    manifest = load_manifest(out_dir)
    known_tags = collect_known_tags(md_dir)
    queue, skipped = build_queue(md_dir, candidates, pages_already_written(md_dir),
                                 manifest, args.force)

    if args.limit:
        before = sum(len(i["pages"]) for i in queue)
        queue = apply_limit(queue, args.limit)
        deferred = before - sum(len(i["pages"]) for i in queue)
        if deferred:
            print(f"Limiting to {args.limit} page(s) this run; {deferred} left for next time.\n")

    if not queue:
        print(f"Nothing new. {len(candidates)} scan(s) present, all processed.")
        for name, why in skipped:
            print(f"  skipped  {name}  ({why})")
        return 0

    total_pages = sum(len(i["pages"]) for i in queue)
    print(f"Input : {drive_root}")
    print(f"Output: {out_dir}")
    print(f"{total_pages} page(s) across {len(queue)} file(s) to process"
          + (f", reusing {len(known_tags)} existing tag(s)" if known_tags else "")
          + "\n")

    if args.dry_run:
        for item in queue:
            pages = ",".join(str(p) for p in item["pages"])
            print(f"  would process  {item['path'].name}  (page {pages} of {item['total']})")
        for name, why in skipped:
            print(f"  skipped        {name}  ({why})")
        return 0

    results: list[dict] = []
    total_cost = 0.0
    done_pages = 0

    with tempfile.TemporaryDirectory(prefix="scan-notes-") as workdir_s:
        workdir = Path(workdir_s)

        for item in queue:
            path, key, multi = item["path"], item["key"], item["total"] > 1
            scan_date = scan_date_from_name(path.name)
            print(f"{path.name}  ({len(item['pages'])} of {item['total']} page(s))")

            try:
                rendered, prep_note = prepare_pages(path, workdir, set(item["pages"]))
            except Exception as e:  # noqa: BLE001 - a bad source must not end the run
                results.append({"src": path.name, "out": "-", "date": "-",
                                "status": "FAILED", "note": str(e)[:160]})
                print(f"  FAILED  {str(e)[:200]}\n")
                continue
            if prep_note:
                print(f"  ({prep_note})")

            segments: list[dict] = []
            failed_pages: list[int] = []
            usage: list[dict] = []
            file_cost = 0.0

            for page_no, image in rendered:
                done_pages += 1
                label = f"  [{done_pages}/{total_pages}]" + (f" p{page_no}" if multi else "")
                print(f"{label} ... ", end="", flush=True)
                started = time.time()
                ctx = context_for(segments, page_no, item["total"]) if multi else None
                try:
                    payload = transcribe(image, claude_bin, scan_date, known_tags,
                                         ctx, args.model)
                except Exception as e:  # noqa: BLE001 - one bad page, not one bad run
                    failed_pages.append(page_no)
                    print(f"FAILED\n        {str(e)[:200]}")
                    continue

                new = payload["segments"]
                for i, seg in enumerate(new):
                    seg["_page"] = page_no
                    seg["_image"] = image
                    # Only a page's first entry can continue the page above, and
                    # nothing can continue when nothing came before it.
                    if i > 0 or not segments:
                        seg["continues_previous"] = False
                    for t in seg.get("tags") or []:
                        t = t.strip().lower()
                        if t and t not in known_tags:
                            known_tags.append(t)
                segments.extend(new)

                file_cost += payload.get("_cost_usd") or 0.0
                usage.append(payload.get("_usage", {}))
                joined = " (continues)" if new[0].get("continues_previous") else ""
                extra = f", {len(new)} entries" if len(new) > 1 else ""
                print(f"ok{joined}{extra}  [{time.time() - started:.0f}s]")

            total_cost += file_cost
            if not segments:
                results.append({"src": path.name, "out": "-", "date": "-",
                                "status": "FAILED", "note": "no page transcribed"})
                print()
                continue

            enforce_date_boundaries(segments)
            written = []
            for note in [assemble_note(r) for r in
                         resolve_dates(group_segments(segments), scan_date,
                                       item.get("seed"))]:
                if not note["body"].strip():
                    # A blank page: transcribed, but nothing was on it. No note,
                    # no image copy - writing either would just be clutter to
                    # clean up later, and the page still counts as done below.
                    span = page_span(note["pages"])
                    results.append({
                        "src": f"{path.name} p{span}" if multi else path.name,
                        "out": "-", "date": "-", "status": "blank",
                        "note": "no text on the page",
                    })
                    print(f"      (blank page{f' {span}' if multi else ''}, no note written)")
                    continue
                md_path, img_paths = claim_names(md_dir, img_dir, note["date"], note["slug"],
                                                 len(note["sources"]))
                note["image_names"] = [p.name for p in img_paths]
                md_path.write_text(build_note(note, path.name, item["total"]),
                                   encoding="utf-8")
                for src_img, dest in zip(note["sources"], img_paths):
                    shutil.copy2(src_img, dest)

                span = page_span(note["pages"])
                written.append({
                    "note": md_path.name,
                    "images": note["image_names"],
                    "pages": span,
                    "date_used": note["date"].isoformat(),
                    "date_source": note["date_source"],
                    "date_on_page": note["date_on_page"],
                    "date_note": note["date_note"] or None,
                    "tags": note["tags"],
                })
                results.append({
                    "src": f"{path.name} p{span}" if multi else path.name,
                    "out": md_path.name,
                    "date": f"{note['date'].isoformat()} ({note['date_source']})",
                    "status": "ok",
                    "note": note["date_note"],
                })
                print(f"      -> {md_path.name}")

            for n in failed_pages:
                results.append({"src": f"{path.name} p{n}", "out": "-", "date": "-",
                                "status": "FAILED", "note": "page transcription failed"})

            # Written once per file, after its notes exist: the manifest is a log,
            # never the thing that decides what still needs doing.
            previous = {} if args.force else manifest.get(key, {})
            save_manifest(out_dir, {key: {
                "original": path.name,
                "pages_total": item["total"],
                "pages_done": sorted(set(previous.get("pages_done", []))
                                     | ({p for p, _ in rendered} - set(failed_pages))),
                "notes": previous.get("notes", []) + written,
                "processed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "usage": usage,
                "cost_usd": round(previous.get("cost_usd", 0.0) + file_cost, 6),
            }})
            print()

    print("=" * 78)
    w = max([len(r["src"]) for r in results] + [len(n) for n, _ in skipped] + [8])
    print(f"{'SOURCE'.ljust(w)}  {'DATE'.ljust(22)}  STATUS   OUTPUT")
    print("-" * 78)
    for r in results:
        print(f"{r['src'].ljust(w)}  {r['date'].ljust(22)}  {r['status'].ljust(7)}  {r['out']}")
        if r["note"]:
            print(f"{' ' * w}  ! {r['note']}")
    for name, why in skipped:
        print(f"{name.ljust(w)}  {'-'.ljust(22)}  skipped  {why}")
    print("-" * 78)

    ok = sum(1 for r in results if r["status"] == "ok")
    blank = sum(1 for r in results if r["status"] == "blank")
    failed = sum(1 for r in results if r["status"] == "FAILED")
    print(f"{ok} note(s) written, {blank} blank page(s) skipped, {failed} failed, "
          f"{len(skipped)} skipped"
          + (f"  (~${total_cost:.2f} of subscription usage)" if total_cost else ""))
    print(f"Output: {out_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Notes already written are safe; the file in progress "
              "resumes from its first uncovered page next run.")
        sys.exit(130)
