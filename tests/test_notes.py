"""Offline tests: nothing here calls the model or touches the real Drive.

Run from the repo folder with:   py -m unittest discover tests -v

The part of notes.py that calls the model (`transcribe`) is replaced by a
stand-in that hands back whatever each test scripts for a page, and the config
is pointed at a throwaway folder, so `main()` runs end to end for free.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notes  # noqa: E402

SCAN = "Scanned_20260819-1500.pdf"


def seg(markdown="some text", continues=False, title="A title", slug="a-title",
        date_iso=None, flag=False, mid=False):
    """One entry as the model would return it."""
    return {
        "continues_previous": continues, "starts_mid_sentence": mid,
        "title": title, "markdown": markdown, "slug": slug, "tags": ["journal"],
        "keyword": None, "date_raw": date_iso, "date_iso": date_iso,
        "flag_review": flag,
    }


BLANK = dict(markdown="", title="", slug="")


class PipelineCase(unittest.TestCase):
    """Runs main() against a temp folder with a scripted model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.drive = root / "drive"
        self.drive.mkdir()
        self.out = self.drive / "out"
        self.md = self.out / notes.MD_SUBDIR
        self.img = self.out / notes.IMG_SUBDIR
        self.calls: list[int] = []      # pages the stand-in model was asked to read
        self.script: dict[int, object] = {}
        self.total = 1

        def fake_prepare(src, workdir, wanted=None):
            pages = []
            for n in sorted(wanted or range(1, self.total + 1)):
                p = workdir / f"p{n}.jpg"
                p.write_bytes(b"page image")
                pages.append((n, p))
            return pages, None

        def fake_transcribe(image, claude_bin, scan_date, known_tags, ctx=None, model=None):
            page = ctx["page"] if ctx else 1
            self.calls.append(page)
            answer = self.script[page]
            if isinstance(answer, BaseException):
                raise answer
            if callable(answer):
                answer = answer(ctx)
            return {"segments": [dict(s) for s in answer], "_usage": {}, "_cost_usd": 0.01}

        cfg = {"drive_root": self.drive, "output_subpath": Path("out")}
        for target, value in [
            ("load_config", lambda: dict(cfg)),
            ("find_claude", lambda cfg: "claude"),
            ("wait_until_stable", lambda path, **kw: True),
            ("source_page_count", lambda path: self.total),
            ("prepare_pages", fake_prepare),
            ("transcribe", fake_transcribe),
            ("CACHE_DIR", root / "cache"),
        ]:
            patcher = mock.patch.object(notes, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def scan(self, pages: int, name: str = SCAN):
        self.total = pages
        (self.drive / name).write_bytes(b"not really a pdf")

    def run_main(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["notes.py", *argv]), \
                contextlib.redirect_stdout(buf):
            code = notes.main()
        return code, buf.getvalue()

    def notes_written(self) -> dict[str, str]:
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(self.md.glob("*.md"))}

    def pages_lines(self) -> list[str]:
        return sorted(line for text in self.notes_written().values()
                      for line in text.splitlines() if line.startswith("pages:"))


class FailedPageTests(PipelineCase):
    def test_failed_page_is_not_swallowed_by_the_note_around_it(self):
        self.scan(3)
        self.script = {
            1: [seg("first part")],
            2: RuntimeError("model fell over"),
            3: [seg("second part", continues=True)],
        }
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        # Page 3 must not join page 1 across the failed page 2.
        self.assertEqual(self.pages_lines(), ["pages: 1 of 3", "pages: 3 of 3"])

        # The next run retries exactly the page that failed.
        self.calls.clear()
        self.script[2] = [seg("the middle")]
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [2])
        self.assertEqual(self.pages_lines(),
                         ["pages: 1 of 3", "pages: 2 of 3", "pages: 3 of 3"])

    def test_page_after_a_failure_is_told_nothing_comes_before_it(self):
        seen = {}
        self.scan(3)
        self.script = {
            1: [seg("first")],
            2: RuntimeError("boom"),
            3: lambda ctx: seen.update(ctx) or [seg("third")],
        }
        self.run_main()
        self.assertNotIn("prev_page", seen)


class BlankPageTests(PipelineCase):
    def test_entry_continues_across_a_blank_page(self):
        seen = {}
        self.scan(3)
        self.script = {
            1: [seg("Ich ging in die", title="Stadtbummel", slug="city-walk")],
            2: [seg(**BLANK)],
            3: lambda ctx: seen.update(ctx) or [seg("Stadt und dann", continues=True,
                                                    mid=True, title="Stadtbummel")],
        }
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("prev_page"), 1)      # looked through the blank page
        written = self.notes_written()
        self.assertEqual(len(written), 1, out)
        name, text = next(iter(written.items()))
        self.assertIn("city-walk", name)
        self.assertNotIn("untitled", name.lower())
        self.assertIn("# Stadtbummel", text)
        self.assertIn("Ich ging in die Stadt und dann", text)   # joined with a space
        self.assertIn("pages: 1-3 of 3", text)
        # Only the two pages with writing are archived as images.
        self.assertEqual(len(list(self.img.glob("*.jpg"))), 2)

    def test_blank_first_page_does_not_name_the_note_untitled(self):
        self.scan(2)
        self.script = {1: [seg(**BLANK)], 2: [seg("text", continues=True)]}
        self.run_main()
        self.assertEqual(list(self.notes_written()), ["2026-08-19_a-title.md"])

    def test_flagged_page_stays_its_own_note(self):
        self.scan(3)
        self.script = {
            1: [seg("part one")],
            2: [seg(flag=True, **BLANK)],
            3: [seg("part two", continues=True)],
        }
        self.run_main()
        self.assertEqual(self.pages_lines(), ["pages: 1-3 of 3", "pages: 2 of 3"])


class InterruptedRunTests(PipelineCase):
    def test_pages_already_read_are_not_read_again(self):
        self.scan(3)
        self.script = {1: [seg("one")], 2: [seg("two", continues=True)],
                       3: KeyboardInterrupt()}
        with self.assertRaises(KeyboardInterrupt):
            self.run_main()
        self.assertEqual(self.notes_written(), {})

        self.calls.clear()
        self.script[3] = [seg("three", continues=True)]
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [3], out)          # pages 1 and 2 were reused
        self.assertEqual(self.pages_lines(), ["pages: 1-3 of 3"])
        self.assertFalse(notes.CACHE_DIR.exists())      # cleaned up once notes exist

    def test_saved_pages_from_another_model_are_ignored(self):
        self.scan(2)
        self.script = {1: [seg("one")], 2: KeyboardInterrupt()}
        with self.assertRaises(KeyboardInterrupt):
            self.run_main()
        self.calls.clear()
        self.script[2] = [seg("two")]
        self.run_main("--model", "opus")
        self.assertEqual(self.calls, [1, 2])


class RepeatedFailureTests(PipelineCase):
    def test_run_stops_after_three_failures_in_a_row(self):
        self.scan(8)
        self.script = {1: [seg("one")]}
        self.script.update({n: RuntimeError("rate limited") for n in range(2, 9)})
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [1, 2, 3, 4])      # pages 5-8 never attempted
        self.assertIn("Stopped early", out)
        self.assertEqual(self.pages_lines(), ["pages: 1 of 8"])  # page 1 was kept

        self.calls.clear()
        self.script.update({n: [seg(f"page {n}")] for n in range(2, 9)})
        self.run_main()
        self.assertEqual(self.calls, list(range(2, 9)))

    def test_a_success_resets_the_count(self):
        self.scan(6)
        bad = RuntimeError("one-off")
        self.script = {1: bad, 2: bad, 3: [seg("three")], 4: bad, 5: bad, 6: [seg("six")]}
        _, out = self.run_main()
        self.assertEqual(self.calls, [1, 2, 3, 4, 5, 6])
        self.assertNotIn("Stopped early", out)


class ForceTests(PipelineCase):
    def test_force_replaces_notes_instead_of_duplicating_them(self):
        self.scan(2)
        self.script = {1: [seg("old one")], 2: [seg("old two", title="B", slug="b")]}
        self.run_main()
        before = sorted(self.notes_written())

        self.script = {1: [seg("new one")], 2: [seg("new two", title="B", slug="b")]}
        _, out = self.run_main("--force")
        written = self.notes_written()
        self.assertEqual(sorted(written), before, out)   # same names, no -2 copies
        self.assertTrue(any("new one" in t for t in written.values()))
        self.assertEqual(len(list(self.img.glob("*.jpg"))), 2)

        replaced = list((self.out / notes.REPLACED_SUBDIR).glob("*/*"))
        self.assertEqual(sorted(p.suffix for p in replaced), [".jpg", ".jpg", ".md", ".md"])
        self.assertTrue(any("old one" in p.read_text(encoding="utf-8")
                            for p in replaced if p.suffix == ".md"))

    def test_finished_scan_is_skipped_without_being_read(self):
        self.scan(1)
        self.script = {1: [seg("one")]}
        self.run_main()
        with mock.patch.object(notes, "file_key", side_effect=AssertionError("hashed")):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("Nothing new", out)


class JoinTests(unittest.TestCase):
    def test_lowercase_start_joins_with_a_space(self):
        self.assertEqual(notes.join_markdown(["I went to the", "shop and"]),
                         "I went to the shop and")

    def test_capitalised_start_needs_the_model_to_say_so(self):
        chunks = ["Ich ging in die", "Stadt und dann"]
        self.assertEqual(notes.join_markdown(chunks), "Ich ging in die\n\nStadt und dann")
        self.assertEqual(notes.join_markdown(chunks, [False, True]),
                         "Ich ging in die Stadt und dann")

    def test_finished_sentence_is_never_welded(self):
        self.assertEqual(notes.join_markdown(["Done.", "Next thought"], [False, True]),
                         "Done.\n\nNext thought")

    def test_list_item_is_never_welded(self):
        self.assertEqual(notes.join_markdown(["things to buy", "- milk"], [False, True]),
                         "things to buy\n\n- milk")


class FooterTests(unittest.TestCase):
    def test_metadata_is_read_from_the_footer_only(self):
        with tempfile.TemporaryDirectory() as d:
            md = Path(d)
            (md / "n.md").write_text(
                "# T\n\noriginal: not-this.pdf\npages: 7 of 9\ntags: wrong\n\n---\n"
                "tags: right\nscan: n.jpg\npages: 2 of 9\noriginal: real.pdf\n",
                encoding="utf-8")
            self.assertEqual(notes.pages_already_written(md), {"real.pdf": {2}})
            self.assertEqual(notes.collect_known_tags(md), ["right"])


class ImageTests(unittest.TestCase):
    def test_png_is_archived_as_a_real_jpeg_inside_the_work_folder(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            src_dir, work = Path(d, "src"), Path(d, "work")
            src_dir.mkdir()
            work.mkdir()
            src = src_dir / "Scanned_20260819-1500.png"
            Image.new("RGBA", (40, 60), "white").save(src)
            out, note = notes.prepare_image(src, work)
            self.assertEqual(out.parent, work)
            self.assertEqual(out.read_bytes()[:2], b"\xff\xd8")     # JPEG signature
            self.assertEqual(note, "converted to JPEG")

    def test_plain_jpeg_is_copied_unchanged_into_the_work_folder(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            src_dir, work = Path(d, "src"), Path(d, "work")
            src_dir.mkdir()
            work.mkdir()
            src = src_dir / "Scanned_20260819-1500.jpg"
            Image.new("RGB", (40, 60), "white").save(src)
            out, note = notes.prepare_image(src, work)
            self.assertEqual(out.parent, work)
            self.assertEqual(out.read_bytes(), src.read_bytes())
            self.assertIsNone(note)


class FindClaudeTests(unittest.TestCase):
    def test_missing_configured_path_falls_back_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d, "claude.exe")
            real.write_bytes(b"")
            with mock.patch.object(notes.shutil, "which", return_value=None), \
                    mock.patch.object(notes, "CLAUDE_FALLBACKS", [real]), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                found = notes.find_claude({"claude_bin": str(Path(d, "1.2.3", "claude.exe"))})
            self.assertEqual(found, str(real))
            self.assertIn("no longer exists", out.getvalue())

    def test_nothing_anywhere_still_exits(self):
        with mock.patch.object(notes.shutil, "which", return_value=None), \
                mock.patch.object(notes, "CLAUDE_FALLBACKS", []), \
                mock.patch.object(notes, "_desktop_app_claude", return_value=None):
            with self.assertRaises(SystemExit):
                notes.find_claude({"claude_bin": ""})


class DateTests(unittest.TestCase):
    """The existing date rules, pinned down so later changes can't bend them."""

    def groups(self, *dates):
        segs = [dict(seg(date_iso=d), _page=i + 1, _image=Path("x"))
                for i, d in enumerate(dates)]
        return notes.resolve_dates(notes.group_segments(segs), dt.date(2026, 8, 19))

    def test_undated_page_takes_the_date_before_it_else_the_one_after(self):
        r = self.groups(None, "2026-07-26", None)
        self.assertEqual([x["date_source"] for x in r], ["inferred", "page", "carried"])
        self.assertTrue(all(x["date"] == dt.date(2026, 7, 26) for x in r))

    def test_date_after_the_scan_is_rejected(self):
        r = self.groups("2026-09-01")
        self.assertEqual(r[0]["date_source"], "scan")
        self.assertIn("after the scan", r[0]["date_note"])

    def test_dated_entry_never_joins_a_different_date(self):
        segs = [seg(date_iso="2026-07-26"), seg(date_iso="2026-07-27", continues=True)]
        notes.enforce_date_boundaries(segs)
        self.assertFalse(segs[1]["continues_previous"])


if __name__ == "__main__":
    unittest.main()
