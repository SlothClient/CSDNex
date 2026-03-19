# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CSDN article export tool that uses reversed API signatures to batch export articles from a logged-in CSDN session. Exports published articles (public/private/fans-only/VIP), drafts, and audit-status articles as local Markdown files with downloaded images.

## Running the Export

```bash
# Recommended: use .env file with CSDN_COOKIE variable
python scripts/csdn_export_all.py --output exports/csdn_export

# Alternative: pass cookie directly
python scripts/csdn_export_all.py --cookie "your_cookie" --output exports/csdn_export

# Alternative: use cookie file
python scripts/csdn_export_all.py --cookie-file cookie.txt --output exports/csdn_export
```

Optional parameters:
- `--statuses`: Comma-separated list (default: `all_v2,draft,audit`)
- `--page-size`: Articles per page (default: 20)
- `--sleep`: Seconds between requests (default: 0.2)
- `--timeout`: Request timeout in seconds (default: 20)
- `--env-file`: Path to .env file (default: `.env`)

## Architecture

Single-script architecture (`scripts/csdn_export_all.py`):

1. **API Signature** (`_make_signed_headers`, `_build_string_to_sign`, `_sign`)
   - HMAC-SHA256 signing with APP_KEY/APP_SECRET
   - Headers: `X-Ca-Key`, `X-Ca-Nonce`, `X-Ca-Timestamp`, `X-Ca-Signature`, `X-Ca-Signature-Headers`

2. **CSDNExporter class**
   - `fetch_list_status()`: Paginated article list retrieval
   - `fetch_article_detail()`: Individual article content
   - `run()`: Main export pipeline

3. **Article Classification** (`_resolve_bucket`)
   - Published articles split by `read_type`, `isNeedFans`, `isNeedVip` flags
   - Categories: `已发布/公开`, `已发布/私密`, `已发布/粉丝可见`, `已发布/VIP可见`, `草稿`, `审核`

4. **Image Localization** (`_localize_markdown_images`, `_materialize_remote_image`)
   - Downloads images to `.assets/` directory
   - Rewrites Markdown/HTML image links to relative paths
   - Creates placeholder SVG for failed downloads

5. **Content Fallback** (`_html_content_to_markdown_fallback`)
   - Prefers `markdowncontent` field
   - Falls back to HTML `content` field with basic conversion
   - Creates placeholder for empty articles

## Output Structure

```
exports/csdn_export_xxx/
├── articles_full.json          # Complete article metadata
├── articles_summary.csv        # Basic article info
├── articles_classification.csv # Final category audit
├── image_failures.json/csv     # Failed image downloads
└── markdown/
    ├── 已发布/公开/
    ├── 已发布/私密/
    ├── 已发布/粉丝可见/
    ├── 已发布/VIP可见/
    ├── 草稿/
    └── 审核/
```

## Dependencies

- Python 3.10+
- `requests` library

## Security Notes

- `.env` and `*.env` are gitignored
- Never commit CSDN_COOKIE to the repository