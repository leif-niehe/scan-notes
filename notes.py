#!/usr/bin/env python3
"""Turn scanned handwritten notes into Markdown files.

Manual trigger: run it when you're at a machine that syncs Drive.
Reads  <drive_root>/Scanned_*.jpg
Writes <drive_root>/02_Areas/Personal/Reflection AI automated/
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
MANIFEST_NAME = "_manifest.json"

SCAN_RE = re.compile(r"^Scanned_(\d{8})-(\d{4})", re.IGNORECASE)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

# An extracted page date this much older than the scan is treated as a misread.
MAX_BACKDATE_DAYS = 5 * 365
CALL_TIMEOUT_S = 300

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


def claim_names(out_dir: Path, date: dt.date, slug: str, img_ext: str) -> tuple[Path, Path]:
    """Find a free (md, image) filename pair, adding -2, -3 on collision."""
    stem = f"{date.isoformat()}_{slug}"
    n = 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        md = out_dir / f"{stem}{suffix}.md"
        img = out_dir / f"{stem}{suffix}{img_ext}"
        if not md.exists() and not img.exists():
            return md, img
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


def originals_already_written(out_dir: Path) -> set[str]:
    """Source filenames recorded in notes that already exist.

    The output folder, not the manifest, is the authoritative answer to "has
    this been processed". It cannot be lost to a Drive sync conflict.
    """
    seen: set[str] = set()
    for md in out_dir.glob("*.md"):
        try:
            m = ORIGINAL_RE.search(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if m:
            seen.add(m.group(1))
    return seen


# --------------------------------------------------------------------------
# image preparation
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


# --------------------------------------------------------------------------
# the model call - the only provider-specific code in this file
# --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "markdown": {"type": "string"},
        "slug": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "keyword": {"type": ["string", "null"]},
        "date_raw": {"type": ["string", "null"]},
        "date_iso": {"type": ["string", "null"]},
    },
    "required": ["title", "markdown", "slug", "tags", "keyword", "date_raw", "date_iso"],
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


def build_prompt(image: Path, scan_date: dt.date, known_tags: list[str]) -> str:
    vocab = (
        "Tags already used in this archive - reuse these wherever one fits, and only "
        "invent a new tag when none does:\n  " + ", ".join(known_tags) + "\n\n"
        if known_tags else ""
    )
    return f"""Transcribe the handwritten page at {image}

This page was scanned on {scan_date.isoformat()}.

Return these fields:

title
  A short title for the note. If the page has its own heading, use it verbatim.
  If it does not, write a brief descriptive one in the page's own language.

markdown
  The transcription, WITHOUT repeating the title as a heading. Keep the page's
  structure. Do not translate.

slug
  3-6 words, lowercase, hyphen-separated, ENGLISH, from the page content.

tags
  2-5 lowercase ENGLISH topic tags.

{vocab}keyword
  If the very top of the page carries a deliberate one-word label (a category
  marker, not a heading or a date), return it. Otherwise null.

date_raw
  The date written on the page, copied exactly as it appears. Null if the page
  carries no date.

date_iso
  Your reading of that date, using these conventions:
    - A slashed two-number form like 07/26 means MONTH/YEAR -> "2026-07"
    - A dotted form like 26.07. means DAY.MONTH -> "2026-07-26"
    - If no year is written, take it from the scan date above. A note cannot be
      written after it was scanned, so roll back one year if that would happen.
    - "YYYY-MM-DD" when the day is known, "YYYY-MM" when only month and year
      are, null when the page carries no date.
  Do not guess a day that is not written.
"""


def transcribe(image_path: Path, claude_bin: str, scan_date: dt.date,
               known_tags: list[str], model: str | None = None) -> dict:
    """Call the model on one image and return the parsed payload.

    Everything provider-specific lives here. Swapping providers means
    rewriting this function and nothing else.

    The flags matter: --safe-mode, --system-prompt and --tools Read strip the
    default Claude Code context down to just this task, which cut measured
    usage from ~228k tokens per page to ~8k. Do NOT add --bare: it forces
    ANTHROPIC_API_KEY auth and bypasses the Pro subscription.
    """
    cmd = [
        claude_bin, "-p", build_prompt(image_path, scan_date, known_tags),
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

    payload["_usage"] = env.get("usage", {})
    payload["_cost_usd"] = env.get("total_cost_usd")
    return payload


# --------------------------------------------------------------------------
# note assembly
# --------------------------------------------------------------------------

def build_note(payload: dict, date_on_page: str | None,
               image_name: str, original_name: str) -> str:
    """Content first, metadata at the bottom. Not frontmatter, by design."""
    title = (payload.get("title") or "").strip() or "Untitled"
    body = (payload.get("markdown") or "").strip()

    parts = [f"# {title}", "", body, "", "---"]
    keyword = (payload.get("keyword") or "").strip()
    if keyword:
        parts.append(f"keyword: {keyword}")
    tags = [t.strip().lower() for t in payload.get("tags", []) if t and t.strip()]
    if tags:
        parts.append(f"tags: {', '.join(tags)}")
    if date_on_page:
        parts.append(f"date_on_page: {date_on_page}")
    parts.append(f"scan: {image_name}")
    parts.append(f"original: {original_name}")
    return "\n".join(parts) + "\n"


TAGS_RE = re.compile(r"^tags:\s*(.+?)\s*$", re.MULTILINE)


def collect_known_tags(out_dir: Path, limit: int = 40) -> list[str]:
    """Existing vocabulary, most used first, so tags cluster instead of drifting."""
    counts: dict[str, int] = {}
    for md in out_dir.glob("*.md"):
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


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, metavar="N",
                    help="process at most N images this run")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be processed, call nothing, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="reprocess images even if they are already recorded")
    ap.add_argument("--model", help="override the model (e.g. opus, sonnet)")
    args = ap.parse_args()

    cfg = load_config()
    claude_bin = find_claude(cfg)
    check_billing()

    drive_root: Path = cfg["drive_root"]
    out_dir = drive_root / OUTPUT_SUBPATH
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = sorted(
        p for p in drive_root.glob("Scanned_*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not candidates:
        print(f"No Scanned_* images in {drive_root}")
        return 0

    manifest = load_manifest(out_dir)
    done_originals = originals_already_written(out_dir)
    known_tags = collect_known_tags(out_dir)

    queue: list[tuple[Path, str]] = []
    skipped: list[tuple[str, str]] = []

    for path in candidates:
        if scan_date_from_name(path.name) is None:
            skipped.append((path.name, "filename has no Scanned_YYYYMMDD-HHMM date"))
            continue
        if not wait_until_stable(path):
            skipped.append((path.name, "still syncing, try again later"))
            continue
        key = file_key(path)
        if not args.force and (key in manifest or path.name in done_originals):
            continue
        queue.append((path, key))

    if args.limit:
        deferred = max(0, len(queue) - args.limit)
        queue = queue[: args.limit]
        if deferred:
            print(f"Limiting to {args.limit} this run; {deferred} left for next time.\n")

    if not queue:
        print(f"Nothing new. {len(candidates)} scan(s) present, all processed.")
        for name, why in skipped:
            print(f"  skipped  {name}  ({why})")
        return 0

    print(f"Input : {drive_root}")
    print(f"Output: {out_dir}")
    print(f"{len(queue)} image(s) to process"
          + (f", reusing {len(known_tags)} existing tag(s)" if known_tags else "")
          + "\n")

    if args.dry_run:
        for path, _ in queue:
            print(f"  would process  {path.name}")
        for name, why in skipped:
            print(f"  skipped        {name}  ({why})")
        return 0

    results: list[dict] = []
    new_entries: dict = {}
    total_cost = 0.0

    with tempfile.TemporaryDirectory(prefix="scan-notes-") as workdir_s:
        workdir = Path(workdir_s)

        for i, (path, key) in enumerate(queue, 1):
            scan_date = scan_date_from_name(path.name)
            print(f"[{i}/{len(queue)}] {path.name} ... ", end="", flush=True)
            started = time.time()

            try:
                image, prep_note = prepare_image(path, workdir)
                if prep_note:
                    print(f"({prep_note}) ", end="", flush=True)

                payload = transcribe(image, claude_bin, scan_date, known_tags, args.model)

                page_date, date_on_page, date_note = parse_page_date(
                    payload.get("date_iso"), scan_date
                )
                use_date = page_date or scan_date
                src = "page" if page_date else "scan"

                slug = slugify(payload.get("slug") or payload.get("title") or "untitled")
                md_path, img_path = claim_names(out_dir, use_date, slug, ".jpg")

                note = build_note(payload, date_on_page, img_path.name, path.name)
                md_path.write_text(note, encoding="utf-8")
                shutil.copy2(image, img_path)

                for t in payload.get("tags", []):
                    t = t.strip().lower()
                    if t and t not in known_tags:
                        known_tags.append(t)

                cost = payload.get("_cost_usd") or 0.0
                total_cost += cost
                new_entries[key] = {
                    "original": path.name,
                    "note": md_path.name,
                    "image": img_path.name,
                    "date_used": use_date.isoformat(),
                    "date_source": src,
                    "date_raw": payload.get("date_raw"),
                    "date_iso": payload.get("date_iso"),
                    "date_note": date_note or None,
                    "tags": payload.get("tags", []),
                    "processed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "usage": payload.get("_usage", {}),
                    "cost_usd": cost,
                }
                # Write after every page: an interrupted run keeps its progress.
                save_manifest(out_dir, new_entries)

                results.append({"src": path.name, "out": md_path.name,
                                "date": f"{use_date.isoformat()} ({src})",
                                "status": "ok", "note": date_note})
                print(f"-> {md_path.name}  [{time.time() - started:.0f}s]")
                if date_note:
                    print(f"        note: {date_note}, used scan date")

            except Exception as e:  # noqa: BLE001 - one bad page must not end the run
                results.append({"src": path.name, "out": "-", "date": "-",
                                "status": "FAILED", "note": str(e)[:160]})
                print(f"FAILED\n        {str(e)[:200]}")

    print("\n" + "=" * 78)
    w = max([len(r["src"]) for r in results] + [8])
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
    failed = len(results) - ok
    print(f"{ok} written, {failed} failed, {len(skipped)} skipped"
          + (f"  (~${total_cost:.2f} of subscription usage)" if total_cost else ""))
    print(f"Output: {out_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Progress up to the last completed page is saved.")
        sys.exit(130)
