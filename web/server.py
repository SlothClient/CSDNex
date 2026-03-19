"""
CSDN Export Web Server
Flask backend for the CSDN Export Terminal frontend.
"""

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Generator, Optional

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

# Add parent directory to path to import csdn_export_all
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from csdn_export_all import CSDNExporter, ExportConfig

app = Flask(
    __name__,
    static_folder="static",
    template_folder=".",
    static_url_path="/static",
)

# Store export jobs
jobs: Dict[str, dict] = {}


def stream_export(job_id: str, config: ExportConfig) -> Generator[str, None, None]:
    """Run export and yield SSE-style JSON lines."""
    job = jobs[job_id]
    job["status"] = "running"
    start_time = time.time()

    exporter = CSDNExporter(config)
    stats = {
        "total": 0,
        "exported": 0,
        "images": 0,
        "failed": 0,
        "content_from_html": 0,
        "placeholder": 0,
    }

    # Bucket counter for report
    bucket_counter: Dict[str, int] = {}

    def send_message(msg_type: str, **kwargs) -> str:
        return json.dumps({"type": msg_type, **kwargs}, ensure_ascii=False) + "\n"

    try:
        # Prepare output directory
        config.output_dir.mkdir(parents=True, exist_ok=True)
        md_dir = config.output_dir / "markdown"
        md_dir.mkdir(parents=True, exist_ok=True)

        yield send_message("log", level="info", message=f"📁 Output directory: {config.output_dir}")

        # Setup bucket directories
        bucket_dirs = {}
        fixed_buckets = [
            "已发布/公开",
            "已发布/私密",
            "已发布/粉丝可见",
            "已发布/VIP可见",
            "草稿",
            "审核",
        ]
        from csdn_export_all import _resolve_bucket, _resolve_md_path, _sanitize_title_as_filename
        from csdn_export_all import (
            _localize_markdown_images,
            _append_cover_images,
            _append_image_failures_section,
            _html_content_to_markdown_fallback,
        )

        for bucket in fixed_buckets:
            path = md_dir / bucket
            path.mkdir(parents=True, exist_ok=True)
            bucket_dirs[bucket] = path

        # Fetch article lists
        all_list_rows = []
        yield send_message("log", level="info", message="🔍 Fetching article list...")

        for status in config.statuses:
            yield send_message("log", level="debug", message=f"   Status: {status}")
            rows, count_info = exporter.fetch_list_status(status)
            all_list_rows.extend(rows)
            yield send_message("log", level="success", message=f"   ✓ Found {len(rows)} articles")

        # Deduplicate by article ID
        by_id = {}
        for row in all_list_rows:
            aid = str(row.get("articleId") or row.get("article_id") or "")
            if not aid:
                continue
            if aid not in by_id:
                by_id[aid] = row
            else:
                by_id[aid].update(row)

        stats["total"] = len(by_id)
        yield send_message("stats", stats=stats)
        yield send_message("log", level="info", message=f"📝 Total unique articles: {len(by_id)}")

        if len(by_id) == 0:
            yield send_message("log", level="warning", message="⚠️ No articles found to export")
            yield send_message("complete", data={"totalUnique": 0, "items": []}, message="Export completed with 0 articles.")
            return

        yield send_message("log", level="info", message="🚀 Starting export...")

        # Process each article
        full_articles = []
        used_md_paths = set()
        image_fail_records = []

        for idx, (aid, row) in enumerate(by_id.items(), start=1):
            if job["cancelled"]:
                yield send_message("log", level="warning", message="⚠️ Export cancelled by user")
                break

            # Update progress
            percent = int((idx / len(by_id)) * 100)
            yield send_message(
                "progress",
                current=idx,
                total=len(by_id),
                text=f"正在导出: {idx}/{len(by_id)} ({percent}%)",
            )

            try:
                detail = exporter.fetch_article_detail(aid)
            except Exception as exc:
                yield send_message("log", level="error", message=f"❌ Failed to fetch {aid}: {exc}")
                detail = {"article_id": aid, "_detail_error": str(exc)}

            merged = {**row, **detail}
            merged["articleId"] = aid
            full_articles.append(merged)

            # Determine bucket
            bucket = _resolve_bucket(merged)
            merged["_bucket"] = bucket
            bucket_counter[bucket] = bucket_counter.get(bucket, 0) + 1

            # Resolve markdown path
            title_value = str(merged.get("title", "无标题"))
            filename = _resolve_md_path(
                bucket=bucket,
                title=title_value,
                article_id=aid,
                bucket_dirs=bucket_dirs,
                used=used_md_paths,
            )

            # Get markdown content
            markdown = merged.get("markdowncontent")
            markdown_text = markdown if isinstance(markdown, str) else ""

            if not markdown_text.strip():
                html_content = merged.get("content") if isinstance(merged.get("content"), str) else ""
                fallback_text = _html_content_to_markdown_fallback(html_content)
                if fallback_text.strip():
                    markdown_text = fallback_text
                    stats["content_from_html"] += 1
                else:
                    markdown_text = f"# {title_value}\n\n> 该文章在 CSDN 接口中 `markdowncontent` 与 `content` 均为空。\n"
                    stats["placeholder"] += 1

            # Localize images
            markdown_text, d_ok, d_fail, body_failures = _localize_markdown_images(
                markdown=markdown_text,
                md_path=filename,
                session=exporter.session,
                timeout_sec=config.timeout_sec,
            )
            stats["images"] += d_ok
            stats["failed"] += d_fail

            # Append cover images
            markdown_text, c_ok, c_fail, cover_failures = _append_cover_images(
                markdown=markdown_text,
                cover_urls=merged.get("coverImage", []),
                md_path=filename,
                session=exporter.session,
                timeout_sec=config.timeout_sec,
            )
            stats["images"] += c_ok
            stats["failed"] += c_fail

            # Collect failures
            article_failures = body_failures + cover_failures
            for failure in article_failures:
                image_fail_records.append({
                    "articleId": aid,
                    "title": title_value,
                    "bucket": bucket,
                    "kind": failure.get("kind", "body"),
                    "url": failure.get("url", ""),
                    "reason": failure.get("reason", "unknown"),
                    "mdPath": str(filename),
                })

            # Append failure section
            markdown_text = _append_image_failures_section(markdown_text, article_failures)

            # Write file
            filename.write_text(markdown_text, encoding="utf-8")
            merged["_md_path"] = str(filename)

            stats["exported"] = idx
            yield send_message("stats", stats=stats)

            # Only send essential article info for results table (NOT the full content!)
            article_summary = {
                "articleId": aid,
                "title": title_value[:100] + "..." if len(title_value) > 100 else title_value,
                "_list_status": merged.get("_list_status", ""),
                "_bucket": bucket,
                "viewCount": merged.get("viewCount", 0),
                "postTime": merged.get("postTime", ""),
            }
            yield send_message("result", article=article_summary)

            # Log every 10 articles
            if idx % 10 == 0:
                yield send_message("log", level="info", message=f"📊 已导出 {idx}/{len(by_id)} 篇文章")

        # Save final outputs
        yield send_message("log", level="info", message="💾 Saving final outputs...")

        out_json = config.output_dir / "articles_full.json"
        out_json.write_text(
            json.dumps(
                {
                    "exportedAt": datetime.now().isoformat(timespec="seconds"),
                    "statuses": config.statuses,
                    "totalUnique": len(full_articles),
                    "items": full_articles,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # Save CSV
        import csv
        out_csv = config.output_dir / "articles_summary.csv"
        with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "articleId", "status", "title", "postTime", "read_type",
                "isNeedFans", "isNeedVip", "editorType", "viewCount",
                "diggCount", "collectCount", "commentCount",
            ])
            for item in full_articles:
                writer.writerow([
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
                ])

        # Save classification CSV
        out_class_csv = config.output_dir / "articles_classification.csv"
        with out_class_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "articleId", "list_status", "bucket", "title",
                "read_type", "isNeedFans", "isNeedVip", "isVipArticle", "status",
            ])
            for item in full_articles:
                writer.writerow([
                    item.get("articleId", ""),
                    item.get("_list_status", ""),
                    item.get("_bucket", ""),
                    item.get("title", ""),
                    item.get("read_type", ""),
                    item.get("isNeedFans", ""),
                    item.get("isNeedVip", ""),
                    item.get("isVipArticle", ""),
                    item.get("status", ""),
                ])

        # Save image failures
        out_img_fail = config.output_dir / "image_failures.json"
        out_img_fail.write_text(
            json.dumps({"count": len(image_fail_records), "items": image_fail_records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Calculate duration
        duration = time.time() - start_time
        minutes, seconds = divmod(int(duration), 60)

        job["status"] = "completed"
        job["result"] = {
            "exportedAt": datetime.now().isoformat(timespec="seconds"),
            "statuses": config.statuses,
            "totalUnique": len(full_articles),
            "stats": stats,
            "bucketCounter": bucket_counter,
            "duration": f"{minutes}分{seconds}秒",
            "items": full_articles,
        }

        # Send final report
        yield send_message("log", level="success", message="─" * 40)
        yield send_message("log", level="success", message="📊 导出报告")
        yield send_message("log", level="success", message="─" * 40)
        yield send_message("log", level="info", message=f"   总文章数: {stats['total']}")
        yield send_message("log", level="info", message=f"   导出成功: {stats['exported']}")
        yield send_message("log", level="info", message=f"   图片下载: {stats['images']}")
        yield send_message("log", level="warning", message=f"   图片失败: {stats['failed']}")
        yield send_message("log", level="info", message=f"   HTML回退: {stats['content_from_html']}")
        yield send_message("log", level="info", message=f"   耗时: {minutes}分{seconds}秒")
        yield send_message("log", level="info", message="─" * 40)
        yield send_message("log", level="info", message="📁 分类统计:")
        for bucket, count in sorted(bucket_counter.items()):
            yield send_message("log", level="info", message=f"   {bucket}: {count} 篇")
        yield send_message("log", level="success", message="─" * 40)
        yield send_message("log", level="success", message="✅ 导出完成!")
        yield send_message("log", level="info", message=f"📂 输出目录: {config.output_dir}")

        yield send_message(
            "complete",
            data={
                "exportedAt": job["result"]["exportedAt"],
                "totalUnique": len(full_articles),
                "stats": stats,
                "bucketCounter": bucket_counter,
                "duration": job["result"]["duration"],
                "items": full_articles,
            },
            message=f"Export completed! {stats['exported']} articles exported.",
        )

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"] = str(e)
        yield send_message("log", level="error", message=f"❌ Export failed: {e}")
        yield send_message("log", level="error", message=traceback.format_exc())
        yield send_message("error", message=f"Export failed: {e}")


@app.route("/")
def index():
    """Serve the main page."""
    return send_from_directory(".", "index.html")


@app.route("/api/export", methods=["POST"])
def export():
    """Start an export job and stream results."""
    # Get parameters
    cookie = request.form.get("cookie", "").strip()
    output_dir = request.form.get("output_dir", "./exports/csdn_export").strip()
    statuses = [s.strip() for s in request.form.get("statuses", "all_v2,draft,audit").split(",") if s.strip()]
    page_size = int(request.form.get("page_size", 20))
    sleep = float(request.form.get("sleep", 0.2))
    timeout = int(request.form.get("timeout", 20))

    # Handle cookie file upload
    cookie_file = request.files.get("cookie_file")
    if cookie_file and not cookie:
        cookie = cookie_file.read().decode("utf-8").strip()
        # Parse .env format
        for line in cookie.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip().lstrip("\ufeff")
                value = value.strip()
                if key == "CSDN_COOKIE":
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                        value = value[1:-1]
                    cookie = value.strip()
                    break

    if not cookie:
        return jsonify({"error": "Cookie is required"}), 400

    # Create config
    config = ExportConfig(
        cookie=cookie,
        output_dir=Path(output_dir),
        statuses=statuses,
        page_size=page_size,
        sleep_sec=sleep,
        timeout_sec=timeout,
    )

    # Create job
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "status": "pending",
        "cancelled": False,
        "config": {
            "output_dir": str(output_dir),
            "statuses": statuses,
            "page_size": page_size,
            "sleep": sleep,
            "timeout": timeout,
        },
    }

    def generate():
        for line in stream_export(job_id, config):
            yield line

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    """Get job status."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(jobs[job_id])


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    """Cancel a running job."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    jobs[job_id]["cancelled"] = True
    return jsonify({"message": "Cancellation requested"})


if __name__ == "__main__":
    print("=" * 60)
    print("  CSDN Export Terminal - Web Interface")
    print("=" * 60)
    print()
    print("  Open http://localhost:5000 in your browser")
    print()
    print("  Press Ctrl+C to stop the server")
    print()
    print("=" * 60)

    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)