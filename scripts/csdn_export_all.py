import argparse
import base64
import csv
import hashlib
import hmac
import html
import json
import math
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests

APP_KEY = "203803574"
APP_SECRET = "9znpamsyl2c7cdrr9sas0le9vbc3r6ba"

LIST_API = "https://bizapi.csdn.net/blog/phoenix/console/v1/article/list"
DETAIL_API = "https://bizapi.csdn.net/blog-console-api/v3/editor/getArticle"

DEFAULT_STATUSES = ["all_v2", "draft", "audit"]

PUBLISHED_STATUSES = {"all_v2", "all_v3", "publish", "published"}

MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HTML_IMG_RE = re.compile(r"(<img\b[^>]*\bsrc=)(['\"])([^'\"]+)(\2)", re.IGNORECASE)
FENCE_LINE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def _rewrite_outside_fenced_code(markdown: str, rewrite_fn) -> str:
    if not markdown:
        return markdown

    out: List[str] = []
    text_buf: List[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    def flush_text() -> None:
        if text_buf:
            out.append(rewrite_fn("".join(text_buf)))
            text_buf.clear()

    for line in markdown.splitlines(keepends=True):
        line_no_indent = line.lstrip(" \t")
        marker_match = FENCE_LINE_RE.match(line_no_indent)

        if marker_match:
            marker = marker_match.group(1)
            marker_char = marker[0]
            marker_len = len(marker)

            if not in_fence:
                flush_text()
                in_fence = True
                fence_char = marker_char
                fence_len = marker_len
                out.append(line)
                continue

            if marker_char == fence_char and marker_len >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
                out.append(line)
                continue

        if in_fence:
            out.append(line)
        else:
            text_buf.append(line)

    flush_text()
    return "".join(out)


def _sanitize_title_as_filename(title: str, max_len: int = 120) -> str:
    name = (title or "").strip()
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "无标题"
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


def _to_bool_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _resolve_bucket(item: Dict) -> str:
    status = str(item.get("_list_status", "")).strip().lower()
    if status == "draft":
        return "草稿"
    if status == "audit":
        return "审核"

    if status in PUBLISHED_STATUSES:
        read_type = str(item.get("read_type", "")).strip().lower()
        need_fans = _to_bool_flag(item.get("isNeedFans"))
        need_vip = _to_bool_flag(item.get("isNeedVip")) or _to_bool_flag(item.get("isVipArticle"))
        vip_read_types = {"read_need_vip", "need_vip", "vip", "vip_only"}
        fans_read_types = {"read_need_fans", "need_fans", "fans_only"}

        if need_vip or read_type in vip_read_types:
            return "已发布/VIP可见"

        if need_fans or read_type in fans_read_types:
            return "已发布/粉丝可见"

        if read_type == "private":
            return "已发布/私密"

        is_public = read_type in {"", "public"}
        return "已发布/公开" if is_public else "已发布/私密"

    return f"其他/{status or 'unknown'}"


def _resolve_md_path(bucket: str, title: str, article_id: str, bucket_dirs: Dict[str, Path], used: Set[str]) -> Path:
    base_dir = bucket_dirs[bucket]

    base_name = _sanitize_title_as_filename(title)
    candidate = base_dir / f"{base_name}.md"
    key = str(candidate).lower()

    if key not in used:
        used.add(key)
        return candidate

    candidate = base_dir / f"{base_name}_{article_id}.md"
    key = str(candidate).lower()
    if key not in used:
        used.add(key)
        return candidate

    seq = 2
    while True:
        candidate = base_dir / f"{base_name}_{article_id}_{seq}.md"
        key = str(candidate).lower()
        if key not in used:
            used.add(key)
            return candidate
        seq += 1


def _is_remote_image_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"}


def _guess_ext(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif"}:
        return suffix

    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        ext = mimetypes.guess_extension(ct) or ""
        if ext == ".jpe":
            ext = ".jpg"
        if ext:
            return ext

    return ".png"


def _ensure_placeholder_image(assets_dir: Path) -> str:
    assets_dir.mkdir(parents=True, exist_ok=True)
    placeholder = assets_dir / "_image_failed.svg"
    if not placeholder.exists():
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'>"
            "<rect width='640' height='360' fill='#f4f4f5'/>"
            "<rect x='20' y='20' width='600' height='320' fill='none' stroke='#d4d4d8' stroke-width='2'/>"
            "<text x='320' y='170' text-anchor='middle' font-size='24' fill='#52525b'>图片下载失败</text>"
            "<text x='320' y='205' text-anchor='middle' font-size='14' fill='#71717a'>请查看文末“图片下载失败清单”中的原始URL</text>"
            "</svg>"
        )
        placeholder.write_text(svg, encoding="utf-8")
    return f"{assets_dir.name}/{placeholder.name}"


def _download_one_image(session: requests.Session, url: str, target_path: Path, timeout_sec: int) -> Tuple[Optional[Path], Optional[str]]:
    request_headers_candidates = [
        {"Referer": "https://blog.csdn.net/", "Origin": "https://blog.csdn.net"},
        {"Referer": "https://mp.csdn.net/", "Origin": "https://mp.csdn.net"},
        {},
    ]

    last_error: Optional[str] = None
    for _ in range(2):
        for extra_headers in request_headers_candidates:
            try:
                with session.get(url, timeout=timeout_sec, stream=True, headers=extra_headers) as resp:
                    if resp.status_code != 200:
                        last_error = f"http_{resp.status_code}"
                        continue

                    content_type = resp.headers.get("Content-Type", "")
                    ext = _guess_ext(url, content_type)
                    final_path = target_path.with_suffix(ext)
                    final_path.parent.mkdir(parents=True, exist_ok=True)

                    size = 0
                    with final_path.open("wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                size += len(chunk)

                    if size <= 0:
                        try:
                            final_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        last_error = "empty_body"
                        continue

                    return final_path, None
            except Exception as exc:
                last_error = f"exception:{type(exc).__name__}"
                continue

    return None, last_error or "unknown"


def _materialize_remote_image(
    url_raw: str,
    assets_dir: Path,
    session: requests.Session,
    timeout_sec: int,
    url_to_rel: Dict[str, str],
) -> Tuple[Optional[str], bool, Optional[str]]:
    url = (url_raw or "").strip().strip("<>")
    url = re.sub(r"\s+", "", url)
    if not _is_remote_image_url(url):
        return None, False, "non_remote_url"

    if url in url_to_rel:
        return url_to_rel[url], False, None

    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    existing = next(assets_dir.glob(f"{digest}.*"), None)
    if existing is not None:
        rel_existing = f"{assets_dir.name}/{existing.name}"
        url_to_rel[url] = rel_existing
        return rel_existing, False, None

    temp_path = assets_dir / digest
    downloaded_path, error_reason = _download_one_image(session, url, temp_path, timeout_sec=timeout_sec)
    if downloaded_path is None:
        return None, False, error_reason

    rel = f"{assets_dir.name}/{downloaded_path.name}"
    url_to_rel[url] = rel
    return rel, True, None


def _html_content_to_markdown_fallback(html_content: str) -> str:
    text = (html_content or "").strip()
    if not text:
        return ""

    normalized = text
    normalized = re.sub(r"<\s*br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*/\s*p\s*>", "\n\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*p\b[^>]*>", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*/\s*div\s*>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*div\b[^>]*>", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*h([1-6])\b[^>]*>(.*?)<\s*/\s*h\1\s*>", lambda m: "\n" + ("#" * int(m.group(1))) + " " + m.group(2) + "\n", normalized, flags=re.IGNORECASE | re.DOTALL)
    normalized = re.sub(r"<\s*li\b[^>]*>", "- ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*/\s*li\s*>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*/\s*ul\s*>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<\s*/\s*ol\s*>", "\n", normalized, flags=re.IGNORECASE)

    # 保留 img 标签给后续本地化逻辑处理，其余标签尽量移除
    placeholder_prefix = "__IMG_PLACEHOLDER_"
    img_tags: List[str] = []

    def hold_img(match: re.Match) -> str:
        idx = len(img_tags)
        img_tags.append(match.group(0))
        return f"{placeholder_prefix}{idx}__"

    normalized = re.sub(r"<img\b[^>]*>", hold_img, normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<[^>]+>", "", normalized)
    normalized = html.unescape(normalized)

    for idx, img_tag in enumerate(img_tags):
        normalized = normalized.replace(f"{placeholder_prefix}{idx}__", img_tag)

    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized


def _localize_markdown_images(markdown: str, md_path: Path, session: requests.Session, timeout_sec: int) -> Tuple[str, int, int, List[Dict]]:
    if not markdown:
        return markdown, 0, 0, []

    assets_dir = md_path.with_suffix(".assets")
    url_to_rel: Dict[str, str] = {}
    downloaded = 0
    failed = 0
    failures: List[Dict] = []

    def ensure_local(url_raw: str) -> str:
        nonlocal downloaded, failed
        url = (url_raw or "").strip().strip("<>")
        if not _is_remote_image_url(url):
            return url_raw

        rel, is_new, error_reason = _materialize_remote_image(
            url_raw=url,
            assets_dir=assets_dir,
            session=session,
            timeout_sec=timeout_sec,
            url_to_rel=url_to_rel,
        )
        if rel is None:
            failed += 1
            placeholder_rel = _ensure_placeholder_image(assets_dir)
            failures.append({"url": url, "reason": error_reason or "unknown", "kind": "body"})
            return placeholder_rel

        if is_new:
            downloaded += 1
        return rel

    def md_repl(match: re.Match) -> str:
        alt = match.group(1)
        inside = match.group(2).strip()
        title_suffix = ""

        if inside.startswith("<") and inside.endswith(">"):
            target = inside[1:-1]
        else:
            target = inside

        if ' "' in target:
            maybe_url, maybe_title = target.split(' "', 1)
            if maybe_title.endswith('"'):
                target = maybe_url
                title_suffix = f' "{maybe_title[:-1]}"'

        new_target = ensure_local(target)
        return f"![{alt}]({new_target}{title_suffix})"

    def html_repl(match: re.Match) -> str:
        prefix = match.group(1)
        quote = match.group(2)
        src = match.group(3)
        suffix_quote = match.group(4)
        new_src = ensure_local(src)
        return f"{prefix}{quote}{new_src}{suffix_quote}"

    def rewrite_chunk(chunk: str) -> str:
        updated = MD_IMAGE_RE.sub(md_repl, chunk)
        updated = HTML_IMG_RE.sub(html_repl, updated)
        return updated

    rewritten = _rewrite_outside_fenced_code(markdown, rewrite_chunk)
    return rewritten, downloaded, failed, failures


def _append_cover_images(
    markdown: str,
    cover_urls,
    md_path: Path,
    session: requests.Session,
    timeout_sec: int,
) -> Tuple[str, int, int, List[Dict]]:
    if not cover_urls:
        return markdown, 0, 0, []

    if isinstance(cover_urls, str):
        cover_list = [cover_urls]
    elif isinstance(cover_urls, list):
        cover_list = [str(u) for u in cover_urls if str(u or "").strip()]
    else:
        cover_list = []

    if not cover_list:
        return markdown, 0, 0, []

    assets_dir = md_path.with_suffix(".assets")
    url_to_rel: Dict[str, str] = {}
    downloaded = 0
    failed = 0
    failures: List[Dict] = []
    lines: List[str] = []

    seen = set()
    for idx, url in enumerate(cover_list, start=1):
        clean_url = str(url or "").strip()
        if not clean_url or clean_url in seen:
            continue
        seen.add(clean_url)

        rel, is_new, error_reason = _materialize_remote_image(
            url_raw=clean_url,
            assets_dir=assets_dir,
            session=session,
            timeout_sec=timeout_sec,
            url_to_rel=url_to_rel,
        )
        if rel is None:
            failed += 1
            failures.append({"url": clean_url, "reason": error_reason or "unknown", "kind": "cover"})
            rel = _ensure_placeholder_image(assets_dir)

        if is_new:
            downloaded += 1
        lines.append(f"![cover_{idx}]({rel})")

    if not lines:
        return markdown, downloaded, failed, failures

    merged = markdown.rstrip() + "\n\n---\n\n## Cover 图\n\n" + "\n\n".join(lines) + "\n"
    return merged, downloaded, failed, failures


def _append_image_failures_section(markdown: str, failures: List[Dict]) -> str:
    if not failures:
        return markdown

    lines = ["", "---", "", "## 图片下载失败清单", ""]
    for idx, item in enumerate(failures, start=1):
        kind = item.get("kind", "body")
        reason = item.get("reason", "unknown")
        url = item.get("url", "")
        lines.append(f"{idx}. [{kind}] {reason} - {url}")

    return markdown.rstrip() + "\n" + "\n".join(lines) + "\n"


def _extract_x_ca_headers(headers: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if not lower_key.startswith("x-ca-"):
            continue
        if lower_key in {
            "x-ca-timestamp",
            "x-ca-signature",
            "x-ca-signature-headers",
            "x-ca-key",
            "x-ca-nonce",
            "x-ca-signed-content-type",
        }:
            out[lower_key] = str(value)
    return out


def _normalize_api_path(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    if host.endswith(".csdn.net"):
        return path
    return path


def _canonical_query(path: str, params: Dict[str, str]) -> str:
    clean_params = {k: v for k, v in params.items() if k != "undefined"}
    if not clean_params:
        return path
    sorted_items = sorted(clean_params.items(), key=lambda kv: kv[0])
    joined = "&".join([f"{k}={v}" if v != "" else f"{k}" for k, v in sorted_items])
    return f"{path}?{joined}" if joined else path


def _build_string_to_sign(
    method: str,
    url: str,
    accept: str,
    date_value: str,
    content_type: str,
    params: Dict[str, str],
    headers: Dict[str, str],
) -> str:
    lines: List[str] = []
    lines.append(method.upper())
    lines.append(accept)
    lines.append("")
    lines.append(content_type)
    lines.append(date_value)

    x_ca_headers = _extract_x_ca_headers(headers)
    for key in sorted(x_ca_headers.keys()):
        lines.append(f"{key}:{x_ca_headers[key]}")

    path = _normalize_api_path(url)
    lines.append(_canonical_query(path, params))

    return "\n".join(lines)


def _sign(string_to_sign: str, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _make_signed_headers(method: str, url: str, params: Dict[str, str], content_type: str = "") -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Accept": "application/json, text/plain, */*",
        "X-Ca-Key": APP_KEY,
        "X-Ca-Nonce": str(uuid.uuid4()),
        "X-Ca-Timestamp": str(int(time.time() * 1000)),
    }

    string_to_sign = _build_string_to_sign(
        method=method,
        url=url,
        accept=headers["Accept"],
        date_value="",
        content_type=content_type,
        params=params,
        headers=headers,
    )

    headers["X-Ca-Signature"] = _sign(string_to_sign, APP_SECRET)

    x_ca_headers = _extract_x_ca_headers(headers)
    signed_header_names = [k for k in sorted(x_ca_headers.keys()) if k != "x-ca-signature"]
    headers["X-Ca-Signature-Headers"] = ",".join(signed_header_names)

    return headers


@dataclass
class ExportConfig:
    cookie: str
    output_dir: Path
    statuses: List[str]
    page_size: int
    sleep_sec: float
    timeout_sec: int


class CSDNExporter:
    def __init__(self, config: ExportConfig):
        self.cfg = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                "Origin": "https://mp.csdn.net",
                "Referer": "https://mp.csdn.net/",
                "Cookie": self.cfg.cookie,
            }
        )

    def _request_json(self, method: str, url: str, params: Dict[str, str]) -> Dict:
        signed_headers = _make_signed_headers(method=method, url=url, params=params)
        headers = {**signed_headers}
        resp = self.session.request(
            method=method,
            url=url,
            params=params,
            headers=headers,
            timeout=self.cfg.timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise RuntimeError(f"API error: {data}")
        return data

    def fetch_list_status(self, status: str) -> Tuple[List[Dict], Dict]:
        page = 1
        all_items: List[Dict] = []
        first_count: Optional[Dict] = None

        while True:
            params = {
                "page": str(page),
                "status": status,
                "pageSize": str(self.cfg.page_size),
            }
            data = self._request_json("GET", LIST_API, params)
            payload = data["data"]
            first_count = first_count or payload.get("count", {})
            rows = payload.get("list", [])
            total = int(payload.get("total", 0))

            if not rows:
                break

            for row in rows:
                row["_list_status"] = status
                row["_page"] = page
                all_items.append(row)

            max_page = math.ceil(total / self.cfg.page_size) if total else page
            print(f"[list] status={status} page={page}/{max_page} rows={len(rows)} total={total}")

            if page >= max_page:
                break
            page += 1
            time.sleep(self.cfg.sleep_sec)

        return all_items, (first_count or {})

    def fetch_article_detail(self, article_id: str) -> Dict:
        params = {
            "id": str(article_id),
            "model_type": "",
        }
        data = self._request_json("GET", DETAIL_API, params)
        return data.get("data", {})

    def run(self) -> None:
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        md_dir = self.cfg.output_dir / "markdown"
        md_dir.mkdir(parents=True, exist_ok=True)
        bucket_dirs: Dict[str, Path] = {}
        fixed_buckets = [
            "已发布/公开",
            "已发布/私密",
            "已发布/粉丝可见",
            "已发布/VIP可见",
            "草稿",
            "审核",
        ]

        def ensure_bucket_dir(bucket: str) -> Path:
            if bucket not in bucket_dirs:
                path = md_dir / bucket
                path.mkdir(parents=True, exist_ok=True)
                bucket_dirs[bucket] = path
            return bucket_dirs[bucket]

        for bucket in fixed_buckets:
            ensure_bucket_dir(bucket)

        all_list_rows: List[Dict] = []
        merged_count: Dict = {}

        for status in self.cfg.statuses:
            rows, count_info = self.fetch_list_status(status)
            all_list_rows.extend(rows)
            merged_count.update(count_info)
            time.sleep(self.cfg.sleep_sec)

        by_id: Dict[str, Dict] = {}
        for row in all_list_rows:
            aid = str(row.get("articleId") or row.get("article_id") or "")
            if not aid:
                continue
            if aid not in by_id:
                by_id[aid] = row
            else:
                by_id[aid].update(row)

        print(f"[summary] unique articles from lists: {len(by_id)}")

        full_articles: List[Dict] = []
        markdown_written = 0
        markdown_placeholder = 0
        image_downloaded = 0
        image_failed = 0
        cover_downloaded = 0
        cover_failed = 0
        content_from_html = 0
        image_fail_records: List[Dict] = []
        used_md_paths: Set[str] = set()
        bucket_counter: Dict[str, int] = {}
        for idx, (aid, row) in enumerate(by_id.items(), start=1):
            try:
                detail = self.fetch_article_detail(aid)
            except Exception as exc:
                print(f"[detail] {idx}/{len(by_id)} articleId={aid} failed: {exc}")
                detail = {"article_id": aid, "_detail_error": str(exc)}

            merged = {**row, **detail}
            merged["articleId"] = aid
            full_articles.append(merged)

            bucket = _resolve_bucket(merged)
            ensure_bucket_dir(bucket)
            merged["_bucket"] = bucket
            bucket_counter[bucket] = bucket_counter.get(bucket, 0) + 1

            title_value = str(merged.get("title", "无标题"))
            filename = _resolve_md_path(
                bucket=bucket,
                title=title_value,
                article_id=aid,
                bucket_dirs=bucket_dirs,
                used=used_md_paths,
            )

            markdown = merged.get("markdowncontent")
            markdown_text = markdown if isinstance(markdown, str) else ""
            if not markdown_text.strip():
                html_content = merged.get("content") if isinstance(merged.get("content"), str) else ""
                fallback_text = _html_content_to_markdown_fallback(html_content)
                if fallback_text.strip():
                    markdown_text = fallback_text
                    content_from_html += 1
                else:
                    markdown_text = f"# {title_value}\n\n> 该文章在 CSDN 接口中 `markdowncontent` 与 `content` 均为空。\n"
                    markdown_placeholder += 1

            markdown_text, d_ok, d_fail, body_failures = _localize_markdown_images(
                markdown=markdown_text,
                md_path=filename,
                session=self.session,
                timeout_sec=self.cfg.timeout_sec,
            )
            image_downloaded += d_ok
            image_failed += d_fail
            article_failures: List[Dict] = []
            for failure in body_failures:
                article_failures.append(failure)
                image_fail_records.append(
                    {
                        "articleId": aid,
                        "title": title_value,
                        "bucket": bucket,
                        "kind": failure.get("kind", "body"),
                        "url": failure.get("url", ""),
                        "reason": failure.get("reason", "unknown"),
                        "mdPath": str(filename),
                    }
                )

            markdown_text, c_ok, c_fail, cover_failures = _append_cover_images(
                markdown=markdown_text,
                cover_urls=merged.get("coverImage", []),
                md_path=filename,
                session=self.session,
                timeout_sec=self.cfg.timeout_sec,
            )
            cover_downloaded += c_ok
            cover_failed += c_fail
            for failure in cover_failures:
                article_failures.append(failure)
                image_fail_records.append(
                    {
                        "articleId": aid,
                        "title": title_value,
                        "bucket": bucket,
                        "kind": failure.get("kind", "cover"),
                        "url": failure.get("url", ""),
                        "reason": failure.get("reason", "unknown"),
                        "mdPath": str(filename),
                    }
                )

            markdown_text = _append_image_failures_section(markdown_text, article_failures)

            filename.write_text(markdown_text, encoding="utf-8")
            merged["_md_path"] = str(filename)
            markdown_written += 1

            if idx % 10 == 0 or idx == len(by_id):
                print(f"[detail] progress {idx}/{len(by_id)}")
            time.sleep(self.cfg.sleep_sec)

        out_json = self.cfg.output_dir / "articles_full.json"
        out_json.write_text(
            json.dumps(
                {
                    "exportedAt": datetime.now().isoformat(timespec="seconds"),
                    "statuses": self.cfg.statuses,
                    "counts": merged_count,
                    "totalUnique": len(full_articles),
                    "items": full_articles,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        out_csv = self.cfg.output_dir / "articles_summary.csv"
        with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "articleId",
                "status",
                "title",
                "postTime",
                "read_type",
                "isNeedFans",
                "isNeedVip",
                "editorType",
                "viewCount",
                "diggCount",
                "collectCount",
                "commentCount",
            ])
            for item in full_articles:
                writer.writerow(
                    [
                        item.get("articleId", ""),
                        item.get("_list_status", ""),
                        item.get("title", ""),
                        item.get("postTime", ""),
                        item.get("read_type", ""),
                        item.get("isNeedFans", ""),
                        item.get("isNeedVip", ""),
                        item.get("editorType", ""),
                        item.get("viewCount", ""),
                        item.get("diggCount", ""),
                        item.get("collectCount", ""),
                        item.get("commentCount", ""),
                    ]
                )

        out_class_csv = self.cfg.output_dir / "articles_classification.csv"
        with out_class_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "articleId",
                "list_status",
                "bucket",
                "title",
                "read_type",
                "isNeedFans",
                "isNeedVip",
                "isVipArticle",
                "status",
            ])
            for item in full_articles:
                writer.writerow(
                    [
                        item.get("articleId", ""),
                        item.get("_list_status", ""),
                        item.get("_bucket", ""),
                        item.get("title", ""),
                        item.get("read_type", ""),
                        item.get("isNeedFans", ""),
                        item.get("isNeedVip", ""),
                        item.get("isVipArticle", ""),
                        item.get("status", ""),
                    ]
                )

        out_img_fail_json = self.cfg.output_dir / "image_failures.json"
        out_img_fail_json.write_text(
            json.dumps(
                {
                    "count": len(image_fail_records),
                    "items": image_fail_records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        out_img_fail_csv = self.cfg.output_dir / "image_failures.csv"
        with out_img_fail_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["articleId", "title", "bucket", "kind", "url", "reason", "mdPath"])
            for record in image_fail_records:
                writer.writerow(
                    [
                        record.get("articleId", ""),
                        record.get("title", ""),
                        record.get("bucket", ""),
                        record.get("kind", ""),
                        record.get("url", ""),
                        record.get("reason", ""),
                        record.get("mdPath", ""),
                    ]
                )

        print(f"[done] JSON: {out_json}")
        print(f"[done] CSV : {out_csv}")
        print(f"[done] CLS : {out_class_csv}")
        print(f"[done] IMG_FAIL_JSON: {out_img_fail_json}")
        print(f"[done] IMG_FAIL_CSV : {out_img_fail_csv}")
        print(f"[done] MD  : {md_dir}")
        print(f"[done] MD written: {markdown_written}")
        print(f"[done] Content fallback from HTML: {content_from_html}")
        print(f"[done] MD placeholder(empty markdowncontent): {markdown_placeholder}")
        print(f"[done] Image downloaded: {image_downloaded}")
        print(f"[done] Image download failed: {image_failed}")
        print(f"[done] Cover downloaded: {cover_downloaded}")
        print(f"[done] Cover download failed: {cover_failed}")
        for key in sorted(bucket_counter.keys()):
            print(f"[done] bucket {key}: {bucket_counter[key]}")


def _load_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        return Path(args.cookie_file).read_text(encoding="utf-8").strip()

    env_file_path = Path(args.env_file or ".env")
    if env_file_path.exists():
        for raw_line in env_file_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lstrip("\ufeff")
            value = value.strip()
            if key != "CSDN_COOKIE":
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            value = value.strip()
            if value:
                return value

    cookie_from_env = os.getenv("CSDN_COOKIE", "").strip()
    if cookie_from_env:
        return cookie_from_env
    raise ValueError("请通过 --cookie / --cookie-file / .env(CSDN_COOKIE) 或环境变量 CSDN_COOKIE 提供登录 Cookie")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export CSDN articles (published/audit/draft) via reversed signed APIs")
    parser.add_argument("--cookie", type=str, default="", help="完整 Cookie 字符串")
    parser.add_argument("--cookie-file", type=str, default="", help="保存 Cookie 的文本文件路径")
    parser.add_argument("--env-file", type=str, default=".env", help="环境变量文件路径（默认 .env，读取 CSDN_COOKIE）")
    parser.add_argument("--output", type=str, default="", help="输出目录")
    parser.add_argument("--statuses", type=str, default=",".join(DEFAULT_STATUSES), help="逗号分隔状态，如 all_v2,draft,audit")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    cookie = _load_cookie(args)
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    output = Path(args.output) if args.output else Path("exports") / f"csdn_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    cfg = ExportConfig(
        cookie=cookie,
        output_dir=output,
        statuses=statuses,
        page_size=args.page_size,
        sleep_sec=args.sleep,
        timeout_sec=args.timeout,
    )

    exporter = CSDNExporter(cfg)
    exporter.run()


if __name__ == "__main__":
    main()
