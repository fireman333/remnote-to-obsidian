#!/usr/bin/env python3
"""
remnote-to-obsidian — Convert RemNote markdown export to Obsidian vault.

A single-file, zero-dependency Python script that performs three conversion
phases in one run:

  1. Convert  — Transform RemNote markdown syntax to Obsidian format
  2. Images   — Download remote images and replace with local embeds
  3. Dedup    — Collapse duplicated parent files into MOC (Map of Content) pages

Usage:
  python3 remnote_to_obsidian.py <source_dir> <output_dir> [options]

Requirements: Python 3.9+, no external packages.
"""

__version__ = "1.1.0"

import argparse
import hashlib
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Stats:
    # Phase 1: Convert
    files_processed: int = 0
    files_skipped_empty: int = 0
    pdf_stubs_created: int = 0
    links_converted: int = 0
    links_kept_external: int = 0
    links_dz_template: int = 0
    links_empty_path: int = 0
    highlights_converted: int = 0
    portals_removed: int = 0
    metadata_lines_removed: int = 0
    flashcard_markers_removed: int = 0
    flashcards_converted: int = 0
    recursion_lines_removed: int = 0
    empty_bullets_removed: int = 0
    filenames_decoded: int = 0
    aliases_extracted: int = 0
    # Phase 2: Images
    images_found: int = 0
    images_downloaded: int = 0
    images_failed: int = 0
    images_skipped: int = 0
    image_links_replaced: int = 0
    image_files_updated: int = 0
    # Phase 3: Dedup
    dedup_pairs_found: int = 0
    dedup_files_converted: int = 0
    dedup_lines_before: int = 0
    dedup_lines_after: int = 0
    dedup_lines_preserved: int = 0
    # Errors — never silently swallowed
    read_errors: int = 0
    write_errors: int = 0


@dataclass
class Options:
    """Conversion behaviour switches.

    flashcards: how to treat RemNote flashcard markup.
        "preserve" — leave markup untouched (default; never loses information)
        "anki"     — rewrite to flashcards-obsidian / Anki syntax
        "strip"    — delete the markers (pre-1.1 behaviour; loses card semantics)
    portals: "mark" leaves an auditable callout where a Portal block was,
        "remove" deletes it silently (pre-1.1 behaviour).
    template_dirs: directory names treated as template/slot definitions.
    """
    flashcards: str = "preserve"
    portals: str = "mark"
    template_dirs: tuple = ("Dz",)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Convert RemNote → Obsidian Markdown
# ═══════════════════════════════════════════════════════════════════════════════

# --- File index ---------------------------------------------------------------

def build_file_index(source_dir: str) -> dict[str, list[str]]:
    """Build basename → [relative_paths] index for all .md files."""
    index: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(source_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            rel_path = os.path.relpath(os.path.join(root, fname), source_dir)
            basename = fname[:-3]
            index.setdefault(basename, []).append(rel_path)
    return index


# --- PDF metadata detection ---------------------------------------------------

def is_pdf_metadata_file(lines: list[str]) -> bool:
    content = "\n".join(lines)
    return "[Type]();-[Pdf]()" in content


def extract_pdf_name(lines: list[str]) -> str:
    for line in lines:
        m = re.search(r"\[Name\]\(\);-(.+)", line.strip().lstrip("- "))
        if m:
            return m.group(1).strip()
    return ""


def convert_pdf_metadata(lines: list[str], filename: str) -> tuple[str, list[str]]:
    pdf_name = extract_pdf_name(lines) or filename
    content = [
        "---",
        f'title: "PDF Reference - {pdf_name}"',
        "type: pdf-reference",
        f'original_name: "{pdf_name}"',
        "---",
        "",
        "> [!info] PDF Reference",
        f"> This note referenced an uploaded PDF: **{pdf_name}**",
        "> The original PDF was not included in the RemNote export.",
        "",
    ]
    return pdf_name, content


# --- Transformer 1: Remove metadata ------------------------------------------

METADATA_KEYS_REMOVE = {
    "ViewerData", "HasNoTextLayer", "LastReadDate", "ReadPercent",
    "Theme", "Size", "Color", "Data", "PDF", "Status", "Type", "URL",
    "Sources",
}

METADATA_PATTERN = re.compile(
    r"^(\s*-\s*)\[(" + "|".join(METADATA_KEYS_REMOVE) + r")\]\(\);?-?"
)


ALIAS_ITEM_PATTERN = re.compile(r"^(?:\d+\.|[-*])\s*")


def extract_aliases(lines: list[str]) -> list[str]:
    aliases: list[str] = []
    in_aliases = False
    alias_indent = 0
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if re.match(r"-\s*\[Aliases\]\(\)", stripped):
            in_aliases = True
            alias_indent = indent
            continue
        if in_aliases:
            if indent > alias_indent and ALIAS_ITEM_PATTERN.match(stripped):
                alias_text = ALIAS_ITEM_PATTERN.sub("", stripped).strip()
                if alias_text:
                    aliases.append(alias_text)
            elif indent <= alias_indent:
                in_aliases = False
    return aliases


def remove_metadata_lines(lines: list[str], stats: Stats) -> list[str]:
    result: list[str] = []
    skip_until_indent = -1
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if skip_until_indent >= 0:
            if indent > skip_until_indent:
                stats.metadata_lines_removed += 1
                i += 1
                continue
            else:
                skip_until_indent = -1

        if METADATA_PATTERN.match(line):
            skip_until_indent = indent
            stats.metadata_lines_removed += 1
            i += 1
            continue

        if re.match(r"\s*-\s*\[Aliases\]\(\)", stripped):
            skip_until_indent = indent
            stats.metadata_lines_removed += 1
            i += 1
            continue

        result.append(line)
        i += 1
    return result


# --- Transformer 2: Remove portals -------------------------------------------

PORTAL_MARKER = "Portal ---------------------"


def remove_portals(lines: list[str], stats: Stats,
                   mode: str = "mark") -> list[str]:
    """Drop Portal transclusion blocks.

    A Portal mirrors content that also lives at its source Rem, so the block
    itself is redundant. With mode="mark" (default) a callout is left behind so
    the removal stays auditable; mode="remove" deletes it without a trace.
    """
    result: list[str] = []
    skip_until_indent = -1
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if skip_until_indent >= 0:
            if indent > skip_until_indent:
                stats.portals_removed += 1
                continue
            else:
                skip_until_indent = -1

        if PORTAL_MARKER in stripped:
            skip_until_indent = indent
            stats.portals_removed += 1
            if mode == "mark":
                result.append(
                    " " * indent
                    + "- > [!note] RemNote Portal removed — "
                    + "the content lives at its source Rem"
                )
            continue

        if stripped.startswith("-- Avoided infinite recursion --"):
            stats.recursion_lines_removed += 1
            continue

        result.append(line)
    return result


# --- Transformer 3: Convert links --------------------------------------------

LINK_PATTERN = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]*)\)")


def is_template_path(decoded_path: str, current_dir: str,
                     template_dirs: tuple) -> bool:
    resolved = os.path.normpath(os.path.join(current_dir, decoded_path))
    parts = Path(resolved).parts
    return any(d in parts for d in template_dirs)


def convert_links(
    line: str, current_file_rel: str,
    basename_index: dict[str, list[str]], stats: Stats,
    template_dirs: tuple = ("Dz",)
) -> str:
    current_dir = os.path.dirname(current_file_rel)

    def replace_link(m: re.Match) -> str:
        display_text = m.group(1)
        raw_path = m.group(2)

        if not raw_path:
            stats.links_empty_path += 1
            return display_text if display_text else ""

        if raw_path.startswith(("http://", "https://", "www.")):
            stats.links_kept_external += 1
            return m.group(0)

        if raw_path.startswith("#"):
            return m.group(0)

        decoded_path = urllib.parse.unquote(raw_path)

        if "%LOCAL_FILE%" in decoded_path or "%LOCAL_FILE%" in raw_path:
            stats.links_empty_path += 1
            return display_text if display_text else "PDF Reference"

        if is_template_path(decoded_path, current_dir, template_dirs):
            stats.links_dz_template += 1
            return f"**{display_text.strip()}**" if display_text.strip() else ""

        target_path = decoded_path
        if target_path.endswith(".md"):
            target_path = target_path[:-3]

        resolved = os.path.normpath(os.path.join(current_dir, target_path))
        basename = os.path.basename(resolved)

        paths_for_basename = basename_index.get(basename, [])
        if len(paths_for_basename) == 1:
            wikilink_target = basename
        elif len(paths_for_basename) > 1:
            wikilink_target = resolved
        else:
            wikilink_target = basename

        display = display_text.strip()
        if display and display != basename:
            result = f"[[{wikilink_target}|{display}]]"
        else:
            result = f"[[{wikilink_target}]]"

        stats.links_converted += 1
        return result

    return LINK_PATTERN.sub(replace_link, line)


# --- Transformer 4: Convert #[[Tag]] -----------------------------------------

TAG_WIKILINK_PATTERN = re.compile(r"#\[\[([^\]]+)\]\]")


def convert_tag_wikilinks(line: str) -> str:
    return TAG_WIKILINK_PATTERN.sub(r"[[\1]]", line)


# --- Transformer 5: Convert highlights ----------------------------------------

HIGHLIGHT_PATTERN = re.compile(r"\^{2,}(.*?)\^{2,}")
ORPHAN_CARET_PATTERN = re.compile(r"\^{2,}")


def convert_highlights(line: str, stats: Stats) -> str:
    new_line, count = HIGHLIGHT_PATTERN.subn(r"==\1==", line)
    stats.highlights_converted += count
    new_line = ORPHAN_CARET_PATTERN.sub("", new_line)
    return new_line


# --- Transformer 6: Flashcards ------------------------------------------------
#
# RemNote encodes several card types inline. Deleting the markup (the pre-1.1
# behaviour) throws away which lines were cards at all, and RemNote exports
# carry no review history, so a stripped export cannot be rebuilt into a deck
# by hand. Default is therefore "preserve": leave the markup alone.
#
# NOTE: the exact marker set below is derived from RemNote's documented syntax.
# Verify it against your own export before relying on mode="anki".

FLASHCARD_LIST_PATTERN = re.compile(r"\s*>>>\s*$")
FLASHCARD_NUM_PATTERN = re.compile(r"\s*>>\d+\.\s*$")
FLASHCARD_FORWARD_PATTERN = re.compile(r"[ \t]*(?<![>\\])>>(?!>)[ \t]*")
CLOZE_PATTERN = re.compile(r"\{\{(?!c\d+::)([^{}]+)\}\}")


def _to_anki(line: str, stats: Stats) -> str:
    """Rewrite RemNote card markup to flashcards-obsidian / Anki syntax."""
    original = line

    # Cloze: {{text}} -> {{c1::text}} (already-numbered clozes are left alone)
    def _cloze(m: "re.Match") -> str:
        return "{{c1::" + m.group(1) + "}}"

    line = CLOZE_PATTERN.sub(_cloze, line)

    # Trailing list-card markers carry no front/back pair — tag the line instead
    # of deleting it, so the card is still findable after conversion.
    stripped_list = FLASHCARD_LIST_PATTERN.sub("", line)
    if stripped_list != line:
        line = stripped_list.rstrip() + " #card"
    else:
        stripped_num = FLASHCARD_NUM_PATTERN.sub("", line)
        if stripped_num != line:
            line = stripped_num.rstrip() + " #card"

    # Front/back separator: "Q >> A" -> "Q :: A"
    line = FLASHCARD_FORWARD_PATTERN.sub(" :: ", line)

    if line != original:
        stats.flashcards_converted += 1
    return line


def convert_flashcards(line: str, stats: Stats,
                       mode: str = "preserve") -> str:
    if mode == "preserve":
        return line
    if mode == "anki":
        return _to_anki(line, stats)
    # mode == "strip" — legacy behaviour, kept for reproducing old runs
    new_line = FLASHCARD_LIST_PATTERN.sub("", line)
    if new_line != line:
        stats.flashcard_markers_removed += 1
        return new_line
    new_line = FLASHCARD_NUM_PATTERN.sub("", line)
    if new_line != line:
        stats.flashcard_markers_removed += 1
        return new_line
    return line


# --- Transformer 7: Clean empty bullets ---------------------------------------

def clean_empty_bullets(lines: list[str], stats: Stats) -> list[str]:
    while lines and re.match(r"^\s*-\s*$", lines[-1]):
        lines.pop()
        stats.empty_bullets_removed += 1

    result: list[str] = []
    prev_empty = False
    for line in lines:
        is_empty = bool(re.match(r"^\s*-\s*$", line))
        if is_empty:
            if prev_empty:
                stats.empty_bullets_removed += 1
                continue
            prev_empty = True
        else:
            prev_empty = False
        result.append(line)
    return result


# --- Frontmatter generation --------------------------------------------------

def generate_frontmatter(title: str, aliases: list[str]) -> list[str]:
    lines = ["---", f'title: "{title}"']
    if aliases:
        lines.append("aliases:")
        for alias in aliases:
            lines.append(f'  - "{alias}"')
    lines.append("---")
    lines.append("")
    return lines


# --- File processing pipeline ------------------------------------------------

CODE_FENCE_PATTERN = re.compile(r"^\s*(?:-\s*)?(?:```|~~~)")


def process_file(
    source_path: str, rel_path: str,
    basename_index: dict[str, list[str]], stats: Stats,
    options: "Options | None" = None
) -> tuple[str, list[str]]:
    """Process a single file. Returns (rename_hint, content_lines)."""
    with open(source_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    lines = [line.rstrip("\n") for line in lines]
    filename = os.path.basename(rel_path)
    title = filename[:-3] if filename.endswith(".md") else filename
    rename_hint = ""

    if not lines or all(not line.strip() for line in lines):
        stats.files_skipped_empty += 1
        return rename_hint, generate_frontmatter(title, [])

    if is_pdf_metadata_file(lines):
        pdf_name, content = convert_pdf_metadata(lines, title)
        stats.pdf_stubs_created += 1
        if pdf_name and pdf_name != title:
            rename_hint = f"PDF Reference - {pdf_name}"
            if not rename_hint.endswith(".md"):
                rename_hint += ".md"
        return rename_hint, content

    aliases = extract_aliases(lines)
    if aliases:
        stats.aliases_extracted += len(aliases)

    opts = options or Options()

    lines = remove_metadata_lines(lines, stats)
    lines = remove_portals(lines, stats, opts.portals)

    # Fenced code blocks are copied through untouched — their contents are not
    # markdown and must not be rewritten by the transformers below.
    processed: list[str] = []
    in_code_block = False
    for line in lines:
        if CODE_FENCE_PATTERN.match(line):
            in_code_block = not in_code_block
            processed.append(line)
            continue
        if in_code_block:
            processed.append(line)
            continue
        line = convert_links(line, rel_path, basename_index, stats,
                             opts.template_dirs)
        line = convert_tag_wikilinks(line)
        line = convert_highlights(line, stats)
        line = convert_flashcards(line, stats, opts.flashcards)
        processed.append(line)

    processed = clean_empty_bullets(processed, stats)
    frontmatter = generate_frontmatter(title, aliases)
    return rename_hint, frontmatter + processed


# --- Output writing -----------------------------------------------------------

def sanitize_filename(name: str) -> str:
    name = html.unescape(name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name


def write_output(
    output_dir: str, rel_path: str, rename_hint: str,
    content_lines: list[str], stats: Stats
) -> None:
    parts = list(Path(rel_path).parts)
    decoded_parts = [sanitize_filename(p) for p in parts]

    if rename_hint:
        decoded_parts[-1] = sanitize_filename(rename_hint)
        stats.filenames_decoded += 1

    out_rel = os.path.join(*decoded_parts) if len(decoded_parts) > 1 else decoded_parts[0]
    out_path = os.path.join(output_dir, out_rel)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(content_lines))
        if content_lines and content_lines[-1] != "":
            f.write("\n")


# --- Phase 1 entry point -----------------------------------------------------

def phase1_convert(source_dir: str, output_dir: str, stats: Stats,
                   verbose: bool = False,
                   options: "Options | None" = None) -> None:
    print("=" * 60)
    print("Phase 1: Converting RemNote markdown → Obsidian")
    print("=" * 60)

    basename_index = build_file_index(source_dir)
    total_files = sum(len(v) for v in basename_index.values())
    duplicated = sum(1 for v in basename_index.values() if len(v) > 1)
    print(f"  Found {total_files} markdown files ({duplicated} duplicated basenames)")

    for root, _dirs, files in os.walk(source_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            source_path = os.path.join(root, fname)
            rel_path = os.path.relpath(source_path, source_dir)

            if verbose:
                print(f"    {rel_path}")

            rename_hint, content_lines = process_file(
                source_path, rel_path, basename_index, stats, options
            )
            stats.files_processed += 1
            write_output(output_dir, rel_path, rename_hint, content_lines, stats)

    print(f"  Converted {stats.files_processed} files")
    print(f"  Links: {stats.links_converted} converted, "
          f"{stats.links_kept_external} external kept, "
          f"{stats.links_dz_template} Dz/ templates")
    print(f"  Highlights: {stats.highlights_converted}")
    print(f"  Portal lines removed: {stats.portals_removed}")
    print(f"  Metadata lines removed: {stats.metadata_lines_removed}")
    print(f"  Flashcard markers removed: {stats.flashcard_markers_removed}")
    print(f"  Flashcard lines converted: {stats.flashcards_converted}")
    print(f"  Aliases extracted: {stats.aliases_extracted}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Download Images & Localize Links
# ═══════════════════════════════════════════════════════════════════════════════

IMAGE_URL_PATTERN = re.compile(
    r"!\[([^\]]*)\]\((https://remnote-user-data\.s3\.amazonaws\.com/[^)]+)\)"
)
WIKILINK_URL_PATTERN = re.compile(
    r"\[\[([^\]|]*)\|(https://remnote-user-data\.s3\.amazonaws\.com/"
    r"[^\]]+\.(?:png|jpg|jpeg|gif|svg|webp))\]\]"
)

MAX_WORKERS = 8
RETRY_COUNT = 3
RETRY_DELAY = 2


def scan_image_urls(vault_dir: str, stats: Stats) -> dict[str, set[str]]:
    url_map: dict[str, set[str]] = {}
    for root, _dirs, files in os.walk(vault_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError as e:
                stats.read_errors += 1
                print(f"  WARNING: cannot read {fpath}: {e}", file=sys.stderr)
                continue
            for m in IMAGE_URL_PATTERN.finditer(content):
                url_map.setdefault(m.group(2), set()).add(fpath)
            for m in WIKILINK_URL_PATTERN.finditer(content):
                url_map.setdefault(m.group(2), set()).add(fpath)
    return url_map


def url_to_local_filename(url: str) -> str:
    """Map a remote URL to a unique local filename.

    Two different URLs can end in the same segment (".../a/image.png" and
    ".../b/image.png"), so the digest of the full URL is always part of the
    name — otherwise one image silently overwrites the other, and the
    "already downloaded" check then hands both notes the same picture.
    """
    url_path = url.split("/")[-1].split("?")[0]
    stem, ext = os.path.splitext(url_path)
    ext = ext or ".png"
    digest = hashlib.md5(url.encode()).hexdigest()[:12]
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:100].strip("._-")
    return f"{safe_stem}-{digest}{ext}" if safe_stem else f"{digest}{ext}"


def download_image(url: str, local_path: str) -> tuple[bool, str]:
    for attempt in range(RETRY_COUNT):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (remnote-to-obsidian)"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                with open(local_path, "wb") as f:
                    f.write(resp.read())
                return True, ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, "404 Not Found"
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return False, f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return False, str(e)
    return False, "Max retries exceeded"


def download_all_images(
    url_map: dict[str, set[str]], attachments_dir: str
) -> dict[str, str]:
    os.makedirs(attachments_dir, exist_ok=True)

    tasks: dict[str, str] = {}
    skipped = 0
    for url in url_map:
        local_name = url_to_local_filename(url)
        local_path = os.path.join(attachments_dir, local_name)
        tasks[url] = local_name
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            skipped += 1

    to_download = {
        u: n for u, n in tasks.items()
        if not os.path.exists(os.path.join(attachments_dir, n))
        or os.path.getsize(os.path.join(attachments_dir, n)) == 0
    }

    print(f"  Unique images: {len(tasks)}, "
          f"already cached: {skipped}, to download: {len(to_download)}")

    if not to_download:
        return tasks

    success = 0
    failed_list: list[tuple[str, str]] = []

    def _dl(item: tuple[str, str]) -> tuple[str, str, bool, str]:
        u, n = item
        ok, err = download_image(u, os.path.join(attachments_dir, n))
        return u, n, ok, err

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_dl, item): item for item in to_download.items()}
        for i, future in enumerate(as_completed(futures), 1):
            u, n, ok, err = future.result()
            if ok:
                success += 1
            else:
                failed_list.append((u, err))
                del tasks[u]
            if i % 50 == 0 or i == len(to_download):
                print(f"    Progress: {i}/{len(to_download)} "
                      f"(OK: {success}, Failed: {len(failed_list)})")

    if failed_list:
        print(f"  Failed ({len(failed_list)}):")
        for u, err in failed_list[:10]:
            print(f"    ...{u[-50:]}: {err}")
        if len(failed_list) > 10:
            print(f"    ... and {len(failed_list) - 10} more")

    return tasks


def update_image_links(
    url_map: dict[str, set[str]], url_to_name: dict[str, str], stats: Stats
) -> tuple[int, int]:
    files_to_update: dict[str, set[str]] = {}
    for url, filepaths in url_map.items():
        if url not in url_to_name:
            continue
        for fpath in filepaths:
            files_to_update.setdefault(fpath, set()).add(url)

    files_updated = 0
    links_replaced = 0

    for fpath, urls in files_to_update.items():
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            stats.read_errors += 1
            print(f"  WARNING: cannot read {fpath}: {e}", file=sys.stderr)
            continue

        original = content
        for url in urls:
            local_name = url_to_name[url]
            local_embed = f"![[attachments/{local_name}]]"

            p1 = re.compile(r"!\[[^\]]*\]\(" + re.escape(url) + r"\)")
            content = p1.sub(local_embed, content)

            p2 = re.compile(r"\[\[[^\]|]*\|" + re.escape(url) + r"\]\]")
            content = p2.sub(local_embed, content)

        if content != original:
            try:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(content)
            except OSError as e:
                stats.write_errors += 1
                print(f"  WARNING: cannot write {fpath}: {e}", file=sys.stderr)
                continue
            files_updated += 1
            links_replaced += (
                original.count("remnote-user-data.s3.amazonaws.com")
                - content.count("remnote-user-data.s3.amazonaws.com")
            )

    return files_updated, links_replaced


def phase2_images(output_dir: str, stats: Stats) -> None:
    print("=" * 60)
    print("Phase 2: Downloading images & localizing links")
    print("=" * 60)

    attachments_dir = os.path.join(output_dir, "attachments")

    url_map = scan_image_urls(output_dir, stats)
    stats.images_found = len(url_map)
    total_refs = sum(len(v) for v in url_map.values())
    print(f"  Found {len(url_map)} unique images ({total_refs} references)")

    if not url_map:
        print("  No remote images found — skipping.")
        print()
        return

    url_to_name = download_all_images(url_map, attachments_dir)
    stats.images_downloaded = len(url_to_name)
    stats.images_failed = len(url_map) - len(url_to_name)

    files_updated, links_replaced = update_image_links(
        url_map, url_to_name, stats)
    stats.image_files_updated = files_updated
    stats.image_links_replaced = links_replaced

    print(f"  Files updated: {files_updated}, links replaced: {links_replaced}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3: Deduplicate Parent Files → MOC
# ═══════════════════════════════════════════════════════════════════════════════

def find_parent_child_pairs(vault_dir: str) -> list[tuple[str, str]]:
    pairs = []
    for root, dirs, files in os.walk(vault_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            folder = os.path.join(root, fname[:-3])
            if os.path.isdir(folder):
                pairs.append((fpath, folder))
    pairs.sort(key=lambda p: p[0].count(os.sep), reverse=True)
    return pairs


def parse_frontmatter(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.split("\n")
    fm: dict = {}
    if not lines or lines[0].strip() != "---":
        return fm
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx <= 0:
        return fm
    aliases = []
    in_aliases = False
    for line in lines[1:end_idx]:
        stripped = line.strip()
        if stripped.startswith("title:"):
            fm["title"] = stripped[6:].strip().strip('"').strip("'")
            in_aliases = False
        elif stripped.startswith("aliases:"):
            in_aliases = True
        elif in_aliases and stripped.startswith("- "):
            aliases.append(stripped[2:].strip().strip('"').strip("'"))
        elif stripped and not stripped.startswith("- "):
            # any other top-level key ends the aliases block
            in_aliases = False
    if aliases:
        fm["aliases"] = aliases
    return fm


def normalize_for_compare(line: str) -> str:
    """Strip indentation and bullet markup so aggregated copies compare equal."""
    return re.sub(r"^\s*(?:[-*+]|\d+\.)\s*", "", line).strip()


def read_body_lines(filepath: str) -> list[str]:
    """Return a file's lines with any YAML frontmatter block removed."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return []
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[i + 1:]
    return lines


def collect_child_lines(child_folder: str) -> set[str]:
    """Every normalized line contained anywhere under a child folder."""
    seen: set[str] = set()
    for root, _dirs, files in os.walk(child_folder):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            for line in read_body_lines(os.path.join(root, fname)):
                norm = normalize_for_compare(line)
                if norm:
                    seen.add(norm)
    return seen


def find_parent_only_lines(parent_path: str, child_folder: str) -> list[str]:
    """Parent lines that appear nowhere in the child subtree.

    RemNote's export writes a parent .md that aggregates its children, but a
    parent Rem can also carry content of its own (a summary, a comparison
    table, exam notes) that exists in no child file. Rebuilding the parent
    purely from the folder listing destroys exactly that content, so it is
    carried over into the MOC instead.

    A parent line that happens to be textually identical to some child line is
    treated as aggregated. That is deliberate: the text still exists in the
    child, so nothing is lost.
    """
    child_lines = collect_child_lines(child_folder)
    kept: list[str] = []
    for line in read_body_lines(parent_path):
        norm = normalize_for_compare(line)
        if not norm:
            continue
        if norm in child_lines:
            continue
        kept.append(line.rstrip())
    return kept


def build_moc_content(
    title: str, frontmatter: dict,
    child_files: list[str], child_dirs: list[str],
    folder_rel: str, parent_only: list[str] | None = None
) -> str:
    lines = ["---", f'title: "{title}"']
    aliases = frontmatter.get("aliases", [])
    if isinstance(aliases, list) and aliases:
        lines.append("aliases:")
        for alias in aliases:
            lines.append(f'  - "{alias}"')
    lines.extend(["---", ""])

    dirs_set = set(child_dirs)
    files_basenames = {f[:-3] for f in child_files}
    orphan_dirs = [d for d in child_dirs if d not in files_basenames]

    for f in child_files:
        name = f[:-3]
        lines.append(f"- [[{folder_rel}/{name}|{name}]]")

    for d in orphan_dirs:
        lines.append(f"- {d}/")

    if not child_files and not orphan_dirs:
        lines.append("*(empty)*")

    if parent_only:
        lines.extend(["", "## Notes", ""])
        lines.extend(parent_only)

    lines.append("")
    return "\n".join(lines)


def phase3_dedup(output_dir: str, stats: Stats) -> None:
    print("=" * 60)
    print("Phase 3: Deduplicating parent files → MOC pages")
    print("=" * 60)

    pairs = find_parent_child_pairs(output_dir)
    stats.dedup_pairs_found = len(pairs)
    print(f"  Found {len(pairs)} parent-child pairs")

    if not pairs:
        print("  No duplicated parent files found — skipping.")
        print()
        return

    for parent_path, child_folder in pairs:
        folder_rel = os.path.relpath(child_folder, output_dir)
        fm = parse_frontmatter(parent_path)
        title = fm.get("title", Path(parent_path).stem)
        original_lines = sum(1 for _ in open(parent_path, encoding="utf-8"))

        try:
            entries = sorted(os.listdir(child_folder))
        except OSError:
            continue

        child_files = [e for e in entries
                       if os.path.isfile(os.path.join(child_folder, e))
                       and e.endswith(".md")]
        child_dirs = [e for e in entries
                      if os.path.isdir(os.path.join(child_folder, e))]

        if not child_files and not child_dirs:
            continue

        parent_only = find_parent_only_lines(parent_path, child_folder)
        stats.dedup_lines_preserved += len(parent_only)

        moc = build_moc_content(title, fm, child_files, child_dirs,
                                folder_rel, parent_only)
        moc_lines = moc.count("\n") + 1

        with open(parent_path, "w", encoding="utf-8") as f:
            f.write(moc)

        stats.dedup_lines_before += original_lines
        stats.dedup_lines_after += moc_lines
        stats.dedup_files_converted += 1

    saved = stats.dedup_lines_before - stats.dedup_lines_after
    pct = (saved / stats.dedup_lines_before * 100) if stats.dedup_lines_before else 0
    print(f"  Converted {stats.dedup_files_converted} files to MOC")
    print(f"  Lines: {stats.dedup_lines_before:,} → {stats.dedup_lines_after:,} "
          f"(removed {saved:,}, {pct:.1f}%)")
    print(f"  Parent-only lines preserved: {stats.dedup_lines_preserved:,}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Final Summary
# ═══════════════════════════════════════════════════════════════════════════════

def print_summary(stats: Stats, output_dir: str) -> None:
    print("=" * 60)
    print("Conversion Complete!")
    print("=" * 60)
    print()
    print("Phase 1 — Markdown conversion:")
    print(f"  Files processed:          {stats.files_processed}")
    print(f"  Empty files:              {stats.files_skipped_empty}")
    print(f"  PDF stubs:                {stats.pdf_stubs_created}")
    print(f"  Links converted:          {stats.links_converted}")
    print(f"  Highlights (^^→==):       {stats.highlights_converted}")
    print(f"  Portal lines removed:     {stats.portals_removed}")
    print(f"  Metadata lines removed:   {stats.metadata_lines_removed}")
    print(f"  Flashcard markers removed:{stats.flashcard_markers_removed}")
    print(f"  Flashcard lines converted:{stats.flashcards_converted}")
    print(f"  Aliases extracted:        {stats.aliases_extracted}")
    print()
    print("Phase 2 — Images:")
    print(f"  Images found:             {stats.images_found}")
    print(f"  Images downloaded:        {stats.images_downloaded}")
    print(f"  Images failed:            {stats.images_failed}")
    print(f"  Links localized:          {stats.image_links_replaced}")
    print()
    print("Phase 3 — Deduplication:")
    print(f"  Parent→MOC converted:     {stats.dedup_files_converted}")
    saved = stats.dedup_lines_before - stats.dedup_lines_after
    print(f"  Duplicate lines removed:  {saved:,}")
    print(f"  Parent-only lines kept:   {stats.dedup_lines_preserved:,}")
    print()
    if stats.read_errors or stats.write_errors:
        print(f"WARNING: {stats.read_errors} read error(s), "
              f"{stats.write_errors} write error(s) — see messages above.")
        print()
    print(f"Obsidian vault ready at: {output_dir}")
    print("Open this folder in Obsidian as a new vault to get started.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert RemNote markdown export to an Obsidian vault.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full conversion (convert + images + dedup):
  python3 remnote_to_obsidian.py ~/Downloads/RemNoteExport ~/ObsidianVault

  # Skip image downloading (offline or fast mode):
  python3 remnote_to_obsidian.py ~/Downloads/RemNoteExport ~/ObsidianVault --skip-images

  # Skip deduplication (keep parent files as-is):
  python3 remnote_to_obsidian.py ~/Downloads/RemNoteExport ~/ObsidianVault --skip-dedup

  # Only convert markdown (no images, no dedup):
  python3 remnote_to_obsidian.py ~/Downloads/RemNoteExport ~/ObsidianVault --skip-images --skip-dedup

  # Rewrite flashcards for Anki, and treat Sx/ as a template folder too:
  python3 remnote_to_obsidian.py ~/Downloads/RemNoteExport ~/ObsidianVault --flashcards anki --template-dirs Dz,Sx
""",
    )
    parser.add_argument("source_dir", help="Path to RemNote export directory")
    parser.add_argument("output_dir", help="Path to output Obsidian vault")
    parser.add_argument("--skip-images", action="store_true",
                        help="Skip downloading images (Phase 2)")
    parser.add_argument("--skip-dedup", action="store_true",
                        help="Skip deduplication (Phase 3)")
    parser.add_argument("--flashcards", choices=["preserve", "anki", "strip"],
                        default="preserve",
                        help="Flashcard markup: preserve it (default), rewrite "
                             "to Anki/flashcards-obsidian syntax, or strip the "
                             "markers (loses which lines were cards)")
    parser.add_argument("--portals", choices=["mark", "remove"], default="mark",
                        help="Portal blocks: leave an auditable callout "
                             "(default) or delete without a trace")
    parser.add_argument("--template-dirs", default="Dz",
                        help="Comma-separated folder names holding template "
                             "slot definitions (default: Dz)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print each file being processed")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source_dir)
    output_dir = os.path.abspath(args.output_dir)

    if not os.path.isdir(source_dir):
        print(f"Error: source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    if os.path.abspath(source_dir) == os.path.abspath(output_dir):
        print("Error: source and output directories must be different.",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    print(f"remnote-to-obsidian v{__version__}")
    print(f"Source: {source_dir}")
    print(f"Output: {output_dir}")
    print()

    stats = Stats()
    options = Options(
        flashcards=args.flashcards,
        portals=args.portals,
        template_dirs=tuple(
            d.strip() for d in args.template_dirs.split(",") if d.strip()
        ),
    )

    # Phase 1: Always run
    phase1_convert(source_dir, output_dir, stats, verbose=args.verbose,
                   options=options)

    # Phase 2: Images (optional)
    if not args.skip_images:
        phase2_images(output_dir, stats)
    else:
        print("Phase 2: Skipped (--skip-images)")
        print()

    # Phase 3: Dedup (optional)
    if not args.skip_dedup:
        phase3_dedup(output_dir, stats)
    else:
        print("Phase 3: Skipped (--skip-dedup)")
        print()

    print_summary(stats, output_dir)


if __name__ == "__main__":
    main()
