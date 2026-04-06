# remnote-to-obsidian

Convert a RemNote markdown export into a clean, ready-to-use Obsidian vault.

Single Python file. Zero external dependencies. Python 3.9+.

## What it does

The script runs three phases in one command:

| Phase | What | Details |
|-------|------|---------|
| **1. Convert** | RemNote syntax → Obsidian markdown | Wikilinks, highlights, metadata cleanup, portal removal, flashcard markers |
| **2. Images** | Download remote images → local | Parallel download from RemNote S3, rewrite to `![[attachments/...]]` |
| **3. Dedup** | Parent files → MOC pages | Collapse duplicated parent aggregation files into Map of Content pages |

### Conversion details

| RemNote | Obsidian | Example |
|---------|----------|---------|
| `[text](url-encoded/path.md)` | `[[Note Name\|text]]` | Wikilinks with shortest-path resolution |
| `^^highlight^^` | `==highlight==` | Also handles `^^^` and `^^^^^^` variants |
| `Portal ---------------------` blocks | Removed | Content exists at source location |
| `[Status]();-[Draft]()` etc. | Removed | RemNote-specific metadata |
| `[ViewerData]`, `[ReadPercent]`, ... | Removed | PDF viewer state, read progress |
| `>>>` / `>>N.` flashcard markers | Removed | End-of-line cloze markers |
| `#[[Tag Name]]` | `[[Tag Name]]` | RemNote nested tag syntax |
| `[text](../Dz/Summary.md)` | `**text**` | Template slot references → bold labels |
| `[text]()` empty-path links | `text` | Concept references → plain text |
| `-- Avoided infinite recursion --` | Removed | Portal recursion guards |
| `![](https://remnote-user-data.s3...)` | `![[attachments/file.png]]` | Downloaded locally |
| `%LOCAL_FILE%hash.pdf.md` | PDF reference stub note | With Obsidian callout |
| `&#8211;` in filenames | `–` (decoded) | HTML entities in filenames |
| `[Aliases]()` sub-items | YAML `aliases:` frontmatter | Extracted to Obsidian properties |
| Parent file duplicating children | MOC with `[[wikilinks]]` | 97%+ line reduction |

## Quick start

```bash
# 1. Export from RemNote: Settings → Export → Markdown
# 2. Run the converter:
python3 remnote_to_obsidian.py ~/Downloads/RemNoteExport ~/ObsidianVault

# 3. Open ~/ObsidianVault in Obsidian as a new vault
```

## Usage

```
python3 remnote_to_obsidian.py <source_dir> <output_dir> [options]
```

### Options

| Flag | Description |
|------|-------------|
| `--skip-images` | Skip downloading images (Phase 2). Use for offline or fast conversion. |
| `--skip-dedup` | Skip deduplication (Phase 3). Keep parent files with full content. |
| `--verbose`, `-v` | Print each file being processed. |
| `--version` | Show version number. |

### Examples

```bash
# Full conversion (recommended):
python3 remnote_to_obsidian.py ./RemNoteExport ./MyVault

# Convert only (no image download, no dedup):
python3 remnote_to_obsidian.py ./RemNoteExport ./MyVault --skip-images --skip-dedup

# Convert + dedup, but skip images (e.g. no internet):
python3 remnote_to_obsidian.py ./RemNoteExport ./MyVault --skip-images
```

## Output structure

```
MyVault/
├── attachments/          # Downloaded images (Phase 2)
│   ├── abc123.png
│   └── ...
├── Topic A.md            # MOC page linking to children (Phase 3)
├── Topic A/
│   ├── Subtopic 1.md     # Actual content (Phase 1)
│   └── Subtopic 2.md
├── Standalone Note.md    # Notes without children
└── ...
```

### After opening in Obsidian

Set the attachment folder path for best results:

**Settings → Files & Links → Default location for new attachments → `attachments`**

## How it works

### Phase 1: Markdown conversion

1. Builds a file index (basename → paths) for smart wikilink resolution
2. Each `.md` file goes through a transformer pipeline:
   - Remove RemNote metadata lines (`[ViewerData]`, `[Status]`, etc.)
   - Remove Portal blocks (bidirectional transclusions)
   - Convert `[text](path.md)` links to `[[wikilinks]]`
   - Convert `#[[Tag]]` to `[[Tag]]`
   - Convert `^^highlight^^` to `==highlight==`
   - Remove flashcard markers (`>>>`, `>>N.`)
   - Clean empty bullet lines
3. Generates YAML frontmatter with title and extracted aliases
4. Writes to output directory with sanitized filenames

### Phase 2: Image localization

1. Scans all `.md` files for `remnote-user-data.s3.amazonaws.com` URLs
2. Downloads images in parallel (8 threads, 3 retries each)
3. Saves to `attachments/` folder
4. Rewrites `![](https://...)` → `![[attachments/filename.png]]`

### Phase 3: Deduplication

RemNote exports create a parent `.md` file (e.g., `Topics.md`) that aggregates
all content from the child folder (`Topics/`). This duplicates content massively
(typically 70-98% of vault size).

1. Finds all `.md` files with a matching same-name folder
2. Processes deepest pairs first (bottom-up)
3. Replaces parent content with a MOC page linking to each child

## Tested with

- RemNote markdown export (~2,400 files, medical knowledge base in Traditional Chinese + English)
- Python 3.12 on macOS

## License

MIT
