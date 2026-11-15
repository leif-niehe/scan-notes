#!/usr/bin/env python3
"""Turn scanned handwritten notes into Markdown files.

Manual trigger: run it when you're at a machine that syncs Drive.
Reads  <drive_root>/Scanned_*.{jpg,png,heic,pdf}
Writes <drive_root>/<output_subpath>/, both set in config.json

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

MD_SUBDIR = "Markdown"
IMG_SUBDIR = "JPEG"
MANIFEST_NAME = "_manifest.json"
# Where --force puts the notes it is replacing, so nothing is ever deleted.
REPLACED_SUBDIR = "_replaced"

# Each page's transcription is saved here the moment it arrives, and removed
# once the notes for its file are written. A run stopped part way through a
# file picks these back up instead of paying to read the same pages twice.
# On this machine only, and gitignored: it holds the text of your notes.
CACHE_DIR = REPO / ".page-cache"

# This many pages failing one after another means something is wrong with the
# connection, the login or the rate limit, not with the pages. Stop rather than
# wait out a timeout on every page still in the queue.
MAX_CONSECUTIVE_FAILURES = 3

SCAN_RE = re.compile(r"^Scanned_(\d{8})-(\d{4})", re.IGNORECASE)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
PDF_EXTS = {".pdf"}
SOURCE_EXTS = IMAGE_EXTS | PDF_EXTS

# An extracted page date this much older than the scan is treated as a misread.
MAX_BACKDATE_DAYS = 5 * 365
CALL_TIMEOUT_S = 300

# Every page image this script writes - a rasterised PDF page, a converted HEIC,
# an EXIF-rotated photo - is saved at this long edge. It is the largest image any
# current Claude model reads; anything bigger is downsampled before the model
# sees it, so extra pixels cost storage and buy nothing, now or on a re-transcribe
# later, and it is still ample for reading the archived page by eye. Quality 85 is
# indistinguishable from 95 on handwriting and roughly halves the file; 95 also
# switches off JPEG colour subsampling, a large size premium for no visible gain.
ARCHIVE_LONG_EDGE_PX = 2576
ARCHIVE_JPEG_QUALITY = 85

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

This repo needs to know where Google Drive is mounted on THIS machine, and
which folder inside it the notes belong in. Neither is committed: the mount
path differs per machine, and where you file your own notes is nobody's
business but yours.

  1. Copy config.example.json to config.json
  2. Set "drive_root" to this machine's My Drive path
       Windows : "H:/My Drive"
       macOS   : "/Users/you/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive"
  3. Set "output_subpath" to the folder inside it that the notes go in,
     e.g. "Notes/Handwritten" - it is created if it doesn't exist
  4. Run again

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

    # Deliberately no default. Falling back to some guessed folder would send a
    # run to an empty directory, where nothing looks processed yet, and quietly
    # re-transcribe an archive that already exists.
    sub = cfg.get("output_subpath", "").strip().strip("/\\")
    if not sub:
        sys.exit('config.json is missing "output_subpath" - the folder inside '
                 'drive_root that the notes go in, e.g. "Notes/Handwritten".')
    parts = [p for p in re.split(r"[/\\]+", sub) if p]
    if any(p == ".." for p in parts) or Path(sub).is_absolute():
        sys.exit(f'"output_subpath" must be a folder inside drive_root: {sub}')
    cfg["output_subpath"] = Path(*parts)
    return cfg


def _desktop_app_claude() -> str | None:
    """The copy of the CLI the Claude desktop app keeps for itself on Windows,
    newest first. Its folder name changes with every update, hence the search."""
    base = os.environ.get("LOCALAPPDATA", "")
    if not base:
        return None
    try:
        found = sorted(Path(base, "Claude-Data").glob("*/claude-code/*/*/claude.exe"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return str(found[0]) if found else None


def find_claude(cfg: dict) -> str:
    explicit = cfg.get("claude_bin", "").strip()
    if explicit and Path(explicit).exists():
        return explicit

    # A configured path that has gone missing is usually one with a version
    # number in it, removed by an update. Look elsewhere before giving up.
    found = shutil.which("claude")
    if not found:
        found = next((str(c) for c in CLAUDE_FALLBACKS if c.is_file()), None)
    if not found:
        found = _desktop_app_claude()
    if found:
        if explicit:
            print(f"!! claude_bin in config.json no longer exists: {explicit}\n"
                  f"!! Using {found} instead. Update config.json, or set\n"
                  '!! claude_bin to "" to have it found automatically.\n')
        return found
    if explicit:
        sys.exit(f"claude_bin in config.json does not exist: {explicit}\n"
                 "and the `claude` CLI was not found anywhere else.")
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


def note_footer(text: str) -> str:
    """The metadata block under a note's last `---` line.

    Everything that reads a note back looks only here, so a line in the
    transcription itself that happens to start with "tags:" or "pages:" is
    never mistaken for metadata.
    """
    return text.rpartition("\n---\n")[2]


SCAN_LINE_RE = re.compile(r"^scan:\s*(.+?)\s*$", re.MULTILINE)
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
            text = note_footer(md.read_text(encoding="utf-8"))
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
            text = note_footer(md.read_text(encoding="utf-8"))
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


def set_aside_old_notes(out_dir: Path, md_dir: Path, img_dir: Path, original: str,
                        pages: set[int], stamp: str) -> int:
    """Move the notes a --force run is about to replace, and their page images,
    into _replaced/<stamp>/. Returns how many notes were moved.

    Without this, re-reading a scan leaves the old note in place and writes the
    new one beside it with a -2 on the end. Moved, not deleted: an old note may
    carry corrections made by hand.
    """
    dest = out_dir / REPLACED_SUBDIR / stamp
    moved = 0
    for md in sorted(md_dir.glob("*.md")):
        try:
            footer = note_footer(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        m = ORIGINAL_RE.search(footer)
        if not m or m.group(1) != original:
            continue
        p = PAGES_RE.search(footer)
        covered = (set(range(int(p.group(1)), int(p.group(2) or p.group(1)) + 1))
                   if p else {1})
        if not covered & pages:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        s = SCAN_LINE_RE.search(footer)
        for name in (s.group(1).split(",") if s else []):
            img = img_dir / name.strip()
            if name.strip() and img.is_file():
                shutil.move(str(img), str(dest / img.name))
        shutil.move(str(md), str(dest / md.name))
        moved += 1
    return moved


# --------------------------------------------------------------------------
# pages already read, kept until their notes are written
# --------------------------------------------------------------------------

def _cache_dir(key: str) -> Path:
    return CACHE_DIR / re.sub(r"[^A-Za-z0-9._-]+", "_", key)


def load_cached_page(key: str, page_no: int, model: str | None) -> dict | None:
    """What an interrupted run already read off this page, if anything.
    Ignored when it was read by a different model than this run asks for."""
    try:
        data = json.loads((_cache_dir(key) / f"p{page_no:04d}.json")
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("model") != (model or ""):
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict) or not payload.get("segments"):
        return None
    return payload


def save_cached_page(key: str, page_no: int, model: str | None, payload: dict) -> None:
    try:
        d = _cache_dir(key)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"p{page_no:04d}.json.tmp"
        tmp.write_text(json.dumps({"model": model or "", "payload": payload}),
                       encoding="utf-8")
        os.replace(tmp, d / f"p{page_no:04d}.json")
    except OSError:
        pass    # a convenience; failing to save it must not fail the page


def clear_cached_pages(key: str) -> None:
    shutil.rmtree(_cache_dir(key), ignore_errors=True)
    try:
        CACHE_DIR.rmdir()       # only succeeds once it is empty
    except OSError:
        pass


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

    Returns an untouched copy when no work is needed, so ordinary JPEGs are
    never re-encoded. When we do have to re-encode, the result is written at the
    same long edge and quality as a rasterised PDF page. PNG and HEIC are always
    re-encoded, because every archived page image is saved under a .jpg name.

    Either way the file handed back is inside workdir, never the source itself:
    the model is given read access to the folder its image is in, and that
    should be a scratch folder holding only pages, not the whole Drive.
    """
    ext = src.suffix.lower()
    is_heic = ext in {".heic", ".heif"}
    is_jpeg = ext in {".jpg", ".jpeg"}

    def untouched(note: str | None) -> tuple[Path, str | None]:
        dest = workdir / src.name
        shutil.copy2(src, dest)
        return dest, note

    try:
        from PIL import Image, ImageOps
    except ImportError:
        if is_heic:
            return untouched("HEIC needs Pillow: pip install pillow pillow-heif")
        if not is_jpeg:
            return untouched("PNG needs Pillow to be archived as a JPEG: pip install pillow")
        return untouched(None)

    if is_heic:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            return untouched("HEIC needs pillow-heif: pip install pillow-heif")

    try:
        with Image.open(src) as im:
            orientation = (im.getexif() or {}).get(0x0112, 1)
            if is_jpeg and orientation in (1, 0):
                return untouched(None)
            fixed = ImageOps.exif_transpose(im)
            if fixed.mode not in ("RGB", "L"):
                fixed = fixed.convert("RGB")
            long_edge = max(fixed.size)
            if long_edge > ARCHIVE_LONG_EDGE_PX:
                scale = ARCHIVE_LONG_EDGE_PX / long_edge
                fixed = fixed.resize(
                    (round(fixed.width * scale), round(fixed.height * scale)),
                    Image.LANCZOS)
            out = workdir / (src.name + ".jpg")
            fixed.save(out, "JPEG", quality=ARCHIVE_JPEG_QUALITY)
        return out, ("converted from HEIC" if is_heic else
                     "rotated via EXIF" if is_jpeg else "converted to JPEG")
    except Exception as e:  # noqa: BLE001 - a bad image must not kill the run
        return untouched(f"image preprocessing failed ({e}), using original")


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
            zoom = min(max(ARCHIVE_LONG_EDGE_PX / long_edge, 0.5), 6.0)
            pix = page.get_pixmap(matrix=pm.Matrix(zoom, zoom))
            dest = workdir / f"{slugify(src.stem, 12)}_p{n:03d}.jpg"
            try:
                pix.pil_save(dest, format="JPEG", quality=ARCHIVE_JPEG_QUALITY)
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
        "starts_mid_sentence": {"type": "boolean"},
        "title": {"type": "string"},
        "markdown": {"type": "string"},
        "slug": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "keyword": {"type": ["string", "null"]},
        "date_raw": {"type": ["string", "null"]},
        "date_iso": {"type": ["string", "null"]},
        "flag_review": {"type": "boolean"},
    },
    "required": ["continues_previous", "starts_mid_sentence", "title", "markdown", "slug", "tags",
                 "keyword", "date_raw", "date_iso", "flag_review"],
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
    "Transcribe in the language written; never translate the transcription. "
    "The one exception is the title field, which is always in English. "
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
    alone = "starts_mid_sentence\n  Always false.\n\n"
    continues_rule = (
        "continues_previous\n"
        "  Always false. This page stands alone.\n\n" + alone
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
                "starts_mid_sentence\n"
                "  True only when continues_previous is true AND this entry's first\n"
                "  words finish a sentence that the last lines above left unfinished.\n"
                "  False when it opens a new sentence, paragraph, heading or list item.\n\n"
            )
        else:
            continues_rule = (
                "continues_previous\n"
                "  Always false: this page is being read on its own.\n\n" + alone
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
{previous}If the page is genuinely blank - nothing on it worth keeping, at most a
printed page number or a scanning artifact - return exactly one segment with
markdown, title and slug set to "", tags set to [], keyword, date_raw, date_iso
set to null, and flag_review set to false.

If the page has no handwritten TEXT to transcribe but is not empty either - a
drawing, a doodle, a diagram, an unreadable mark, something showing through
from the other side of the page - do not guess at what it is or invent a
transcription for it. Return exactly one segment the same way as a blank page
(markdown, title, slug "", tags [], the rest null) EXCEPT set flag_review to
true, so the page is kept for a human to look at rather than discarded.
flag_review is otherwise always false.

A page usually holds exactly one journal entry, so usually you return
exactly one segment. Return more than one ONLY where a horizontal divider drawn
on the page is followed by a NEW DATE. A divider on its own does not start a new
entry: keep the text on both sides of it in the same segment, and transcribe the
divider itself as *** on a line of its own. When in doubt, do not split.

Return these fields for every segment:

{continues_rule}title
  A short title for the entry, always in ENGLISH. If it has its own heading in
  English, use it verbatim; if the heading is in another language, translate
  it. If it has no heading, write a brief descriptive English one.
  The title is the only thing translated - the transcription never is.
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


def join_markdown(chunks: list[str], mid_sentence_flags: list[bool] | None = None) -> str:
    """Stitch a note's pages together.

    A page that resumes mid-sentence is joined with a space; anything else gets
    a blank line, so a continuation never silently welds two paragraphs.

    "Resumes mid-sentence" needs the page above to end without closing
    punctuation, and then either the model saying so (mid_sentence_flags, one
    per chunk) or the page starting with a lowercase letter. The lowercase test
    alone misses German, where a sentence often resumes on a capitalised noun.
    """
    out = ""
    for i, chunk in enumerate(chunks):
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        if not out:
            out = chunk
            continue
        last_line = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
        said_so = bool(mid_sentence_flags and i < len(mid_sentence_flags)
                       and mid_sentence_flags[i])
        mid_sentence = (
            OPEN_TAIL_RE.search(last_line.rstrip())
            and not BLOCK_START_RE.match(chunk)
            and (said_so or chunk[:1].islower())
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
            m = TAGS_RE.search(note_footer(md.read_text(encoding="utf-8")))
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


def is_empty_segment(seg: dict) -> bool:
    """A page (or entry) with no transcribed text: blank, or flagged for review."""
    return not (seg.get("markdown") or "").strip()


def group_segments(segments: list[dict]) -> list[list[dict]]:
    """One group per reflection. A group spans pages; a page can start a group.

    A page with no text always stands alone, and is looked straight through: an
    entry that continues does so from the last page that had writing on it, so
    a blank back-of-sheet in the middle of a reflection does not cut it in two.
    """
    groups: list[list[dict]] = []
    target: int | None = None           # the last group that has text in it
    for seg in segments:
        if is_empty_segment(seg):
            groups.append([seg])
        elif target is not None and seg.get("continues_previous"):
            groups[target].append(seg)
        else:
            groups.append([seg])
            target = len(groups) - 1
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
    title = next((s["title"] for s in g if (s.get("title") or "").strip()), "")
    slug = next((s["slug"] for s in g if (s.get("slug") or "").strip()), "")
    return {
        "title": title,
        "body": join_markdown([s.get("markdown") or "" for s in g],
                              [bool(s.get("starts_mid_sentence")) for s in g]),
        "slug": slugify(slug or title or "untitled"),
        "tags": tags[:6],
        "keyword": keyword,
        "flagged": any(seg.get("flag_review") for seg in g),
        "pages": pages,
        "sources": images,
        "date": r["date"],
        "date_source": r["date_source"],
        "date_on_page": r.get("date_on_page"),
        "date_inferred": r.get("date_inferred"),
        "date_note": r.get("date_note") or "",
    }


def context_for(segments: list[dict], page_no: int, total: int) -> dict:
    """What page `page_no` needs to know about the page before it.

    "The page before it" is the last page that had writing on it, provided
    every page in between was read in this run and came back empty. If a page
    in between failed, or was not part of this run, nothing is offered to
    continue from: joining across a gap would write a note whose `pages:` line
    claims the missing page, and that page would then never be retried.
    """
    ctx: dict = {"page": page_no, "total": total}
    if not segments:
        return ctx
    prev = next((s for s in reversed(segments) if not is_empty_segment(s)), None)
    if prev is not None:
        empty_pages = {s["_page"] for s in segments if is_empty_segment(s)}
        if all(n in empty_pages for n in range(prev["_page"] + 1, page_no)):
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

    # Scans the run log records as finished, by name and size. Recognising one
    # this way costs nothing; the check further down has to wait two seconds
    # and read the whole file first, which adds up as old scans pile up in the
    # Drive root. Entries logged before the size was recorded take the slow way.
    finished = {
        (e.get("original"), e.get("size")) for e in manifest.values()
        if isinstance(e, dict) and e.get("size") and e.get("pages_total")
        and len(set(e.get("pages_done") or [])) >= e["pages_total"]
    }

    for path in candidates:
        if scan_date_from_name(path.name) is None:
            skipped.append((path.name, "filename has no Scanned_YYYYMMDD-HHMM date"))
            continue
        if not force:
            try:
                if (path.name, path.stat().st_size) in finished:
                    continue
            except OSError:
                pass
        if not wait_until_stable(path):
            skipped.append((path.name, "still syncing, try again later"))
            continue
        key = file_key(path)
        logged = {} if force else manifest.get(key, {})
        if (logged.get("pages_total")
                and len(pages_from_manifest(manifest, key)) >= logged["pages_total"]):
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
    out_dir = drive_root / cfg["output_subpath"]
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
    consecutive_failures = 0
    stopped_early = False
    run_stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

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
            ok_pages: list[int] = []
            usage: list[dict] = []
            file_cost = 0.0

            for page_no, image in rendered:
                done_pages += 1
                label = f"  [{done_pages}/{total_pages}]" + (f" p{page_no}" if multi else "")
                print(f"{label} ... ", end="", flush=True)
                started = time.time()
                ctx = context_for(segments, page_no, item["total"]) if multi else None

                # A page an interrupted run already read is not read again.
                payload = None if args.force else load_cached_page(key, page_no, args.model)
                reused = payload is not None
                if not reused:
                    try:
                        payload = transcribe(image, claude_bin, scan_date, known_tags,
                                             ctx, args.model)
                    except Exception as e:  # noqa: BLE001 - one bad page, not one bad run
                        failed_pages.append(page_no)
                        consecutive_failures += 1
                        print(f"FAILED\n        {str(e)[:200]}")
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            stopped_early = True
                            break
                        continue
                    consecutive_failures = 0
                    save_cached_page(key, page_no, args.model, payload)
                    total_cost += payload.get("_cost_usd") or 0.0
                ok_pages.append(page_no)

                new = payload["segments"]
                can_continue = bool(ctx and ctx.get("prev_page"))
                for i, seg in enumerate(new):
                    seg["_page"] = page_no
                    seg["_image"] = image
                    # Only a page's first entry can continue the page above, and
                    # only when there is a page above to continue: not across a
                    # page that failed, and not when nothing came before it.
                    if i > 0 or not can_continue:
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
                took = ("already read by an interrupted run" if reused
                        else f"{time.time() - started:.0f}s")
                print(f"ok{joined}{extra}  [{took}]")

            if not segments:
                results.append({"src": path.name, "out": "-", "date": "-",
                                "status": "FAILED", "note": "no page transcribed"})
                print()
                if stopped_early:
                    break
                continue

            if args.force:
                aside = set_aside_old_notes(out_dir, md_dir, img_dir, path.name,
                                            set(ok_pages), run_stamp)
                if aside:
                    print(f"      ({aside} earlier note(s) moved to "
                          f"{REPLACED_SUBDIR}/{run_stamp}/)")

            enforce_date_boundaries(segments)
            written = []
            for note in [assemble_note(r) for r in
                         resolve_dates(group_segments(segments), scan_date,
                                       item.get("seed"))]:
                span = page_span(note["pages"])
                body_text = note["body"].strip()

                if not body_text and not note["flagged"]:
                    # Genuinely blank: transcribed, but nothing was on it. No
                    # note, no image copy - writing either would just be
                    # clutter to clean up later, and the page still counts as
                    # done below.
                    results.append({
                        "src": f"{path.name} p{span}" if multi else path.name,
                        "out": "-", "date": "-", "status": "blank",
                        "note": "no text on the page",
                    })
                    print(f"      (blank page{f' {span}' if multi else ''}, no note written)")
                    continue

                if not body_text and note["flagged"]:
                    # No text, but not nothing either - a drawing, a mark,
                    # bleed-through. Kept, not guessed at, so it can be
                    # checked and discarded by hand if it turns out to be
                    # nothing (see the flagged list at the end of the run).
                    note["title"] = note["title"] or "Flagged page (no text)"
                    note["body"] = (
                        "*(No transcribable text on this page - flagged for manual "
                        "review. Could be a drawing, a mark, or print showing through "
                        "from the other side; check the image and delete this note if "
                        "it isn't worth keeping.)*"
                    )

                md_path, img_paths = claim_names(md_dir, img_dir, note["date"], note["slug"],
                                                 len(note["sources"]))
                note["image_names"] = [p.name for p in img_paths]
                md_path.write_text(build_note(note, path.name, item["total"]),
                                   encoding="utf-8")
                for src_img, dest in zip(note["sources"], img_paths):
                    shutil.copy2(src_img, dest)

                written.append({
                    "note": md_path.name,
                    "images": note["image_names"],
                    "pages": span,
                    "date_used": note["date"].isoformat(),
                    "date_source": note["date_source"],
                    "date_on_page": note["date_on_page"],
                    "date_note": note["date_note"] or None,
                    "tags": note["tags"],
                    "flagged": note["flagged"],
                })
                note_status = "review" if note["flagged"] else "ok"
                note_text = "; ".join(
                    x for x in (note["date_note"],
                                "flagged for manual review" if note["flagged"] else "") if x
                )
                results.append({
                    "src": f"{path.name} p{span}" if multi else path.name,
                    "out": md_path.name,
                    "date": f"{note['date'].isoformat()} ({note['date_source']})",
                    "status": note_status,
                    "note": note_text,
                })
                print(f"      -> {md_path.name}"
                      + ("  (flagged for review)" if note["flagged"] else ""))

            for n in failed_pages:
                results.append({"src": f"{path.name} p{n}", "out": "-", "date": "-",
                                "status": "FAILED", "note": "page transcription failed"})

            # Written once per file, after its notes exist: the manifest is a log,
            # never the thing that decides what still needs doing.
            previous = {} if args.force else manifest.get(key, {})
            save_manifest(out_dir, {key: {
                "original": path.name,
                "size": path.stat().st_size,
                "pages_total": item["total"],
                "pages_done": sorted(set(previous.get("pages_done", [])) | set(ok_pages)),
                "notes": previous.get("notes", []) + written,
                "processed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "usage": usage,
                "cost_usd": round(previous.get("cost_usd", 0.0) + file_cost, 6),
            }})
            # The notes exist now, so the saved pages have done their job.
            clear_cached_pages(key)
            print()
            if stopped_early:
                break

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
    review = [r for r in results if r["status"] == "review"]
    blank = sum(1 for r in results if r["status"] == "blank")
    failed = sum(1 for r in results if r["status"] == "FAILED")
    print(f"{ok} note(s) written, {len(review)} flagged for review, "
          f"{blank} blank page(s) skipped, {failed} failed, {len(skipped)} skipped"
          + (f"  (~${total_cost:.2f} of subscription usage)" if total_cost else ""))
    if stopped_early:
        print()
        print(f"Stopped early: {MAX_CONSECUTIVE_FAILURES} pages in a row failed, which "
              "points at the connection, the login or\nthe rate limit rather than the "
              "pages. Nothing is lost - run again later and it carries on\nfrom the "
              "first page without a note.")
    if review:
        print()
        print("Flagged for manual review - no text, but not blank either "
              "(a drawing, a mark, print showing through):")
        for r in review:
            print(f"  {r['out']}")
        print("Open these, check the page image, and delete the note (and its "
              "image in JPEG/) if it isn't worth keeping.")
    print(f"Output: {out_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Notes already written are safe, and the pages already "
              "read from the file\nin progress are saved: the next run reuses them "
              "and carries on from where this one stopped.")
        sys.exit(130)
