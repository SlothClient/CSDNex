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

    def translate_status(status: str) -> str:
        """Translate status code to Chinese."""
        status_map = {
            "all_v2": "已发布",
            "all_v3": "已发布",
            "publish": "已发布",
            "published": "已发布",
            "draft": "草稿箱",
            "audit": "审核中",
        }
        return status_map.get(status, status)

    def load_existing_articles(output_dir: Path) -> dict:
        """Load existing articles from previous export."""
        existing_json = output_dir / "articles_full.json"
        if existing_json.exists():
            try:
                data = json.loads(existing_json.read_text(encoding="utf-8"))
                items = data.get("items", [])
                # Build dict: articleId -> article data
                return {str(item.get("articleId")): item for item in items if item.get("articleId")}
            except Exception:
                pass
        return {}

    try:
        # Prepare output directory
        config.output_dir.mkdir(parents=True, exist_ok=True)
        md_dir = config.output_dir / "markdown"
        md_dir.mkdir(parents=True, exist_ok=True)

        yield send_message("log", level="info", message=f"📁 输出目录: {config.output_dir}")

        # Load existing articles for incremental update
        existing_articles = load_existing_articles(config.output_dir)
        if existing_articles:
            yield send_message("log", level="info", message=f"📚 发现已有 {len(existing_articles)} 篇导出文章")

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
        yield send_message("log", level="info", message="🔍 正在获取文章列表...")

        for status in config.statuses:
            status_cn = translate_status(status)
            yield send_message("log", level="debug", message=f"   状态: {status_cn}")
            rows, count_info = exporter.fetch_list_status(status)
            all_list_rows.extend(rows)
            yield send_message("log", level="success", message=f"   ✓ 找到 {len(rows)} 篇文章")

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
        yield send_message("log", level="info", message=f"📝 共 {len(by_id)} 篇不重复文章")

        if len(by_id) == 0:
            yield send_message("log", level="warning", message="⚠️ 没有找到可导出的文章")
            yield send_message("complete", data={"totalUnique": 0}, message="导出完成，共 0 篇文章")
            return

        # Filter out already exported articles (incremental update)
        new_article_ids = [aid for aid in by_id.keys() if aid not in existing_articles]
        skipped_count = len(by_id) - len(new_article_ids)

        if skipped_count > 0:
            yield send_message("log", level="info", message=f"⏭️ 跳过已导出的 {skipped_count} 篇文章")

        if len(new_article_ids) == 0:
            yield send_message("log", level="success", message="─" * 40)
            yield send_message("log", level="success", message="✨ 所有内容已经都拉下来啦！")
            yield send_message("log", level="success", message="─" * 40)
            yield send_message("complete", data={
                "totalUnique": len(by_id),
                "newArticles": 0,
                "skipped": skipped_count,
            }, message="无需更新")
            return

        yield send_message("log", level="info", message=f"🆕 需要导出 {len(new_article_ids)} 篇新文章")
        yield send_message("log", level="info", message="🚀 开始导出...")

        # Process each NEW article only
        full_articles = []
        used_md_paths = set()
        image_fail_records = []
        stats["new"] = len(new_article_ids)
        stats["skipped"] = skipped_count

        for idx, aid in enumerate(new_article_ids, start=1):
            row = by_id[aid]
            if job["cancelled"]:
                yield send_message("log", level="warning", message="⚠️ 导出已被用户取消")
                break

            # Update progress
            percent = int((idx / len(new_article_ids)) * 100)
            yield send_message(
                "progress",
                current=idx,
                total=len(new_article_ids),
                text=f"正在导出新文章: {idx}/{len(new_article_ids)} ({percent}%)",
            )

            try:
                detail = exporter.fetch_article_detail(aid)
            except Exception as exc:
                yield send_message("log", level="error", message=f"❌ 获取文章 {aid} 失败: {exc}")
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
                "_list_status_cn": translate_status(merged.get("_list_status", "")),
                "_bucket": bucket,
                "viewCount": merged.get("viewCount", 0),
                "postTime": merged.get("postTime", ""),
            }
            yield send_message("result", article=article_summary)

            # Log every 10 articles
            if idx % 10 == 0:
                yield send_message("log", level="info", message=f"📊 已导出 {idx}/{len(new_article_ids)} 篇新文章")

        # Merge with existing articles
        all_articles = list(existing_articles.values()) + full_articles
        total_articles = len(all_articles)

        # Save final outputs
        yield send_message("log", level="info", message="💾 正在保存导出文件...")

        out_json = config.output_dir / "articles_full.json"
        out_json.write_text(
            json.dumps(
                {
                    "exportedAt": datetime.now().isoformat(timespec="seconds"),
                    "statuses": config.statuses,
                    "totalUnique": total_articles,
                    "newArticles": len(full_articles),
                    "items": all_articles,
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
            for item in all_articles:
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
            for item in all_articles:
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
            "totalUnique": total_articles,
            "newArticles": len(full_articles),
            "stats": stats,
            "bucketCounter": bucket_counter,
            "duration": f"{minutes}分{seconds}秒",
        }

        # Send final report
        yield send_message("log", level="success", message="─" * 40)
        yield send_message("log", level="success", message="📊 导出报告")
        yield send_message("log", level="success", message="─" * 40)
        yield send_message("log", level="info", message=f"   远程文章数: {stats['total']}")
        if skipped_count > 0:
            yield send_message("log", level="info", message=f"   已有文章数: {skipped_count}")
        yield send_message("log", level="success", message=f"   新导出文章: {len(full_articles)}")
        yield send_message("log", level="info", message=f"   本地总计: {total_articles}")
        yield send_message("log", level="info", message=f"   图片下载: {stats['images']}")
        if stats['failed'] > 0:
            yield send_message("log", level="warning", message=f"   图片失败: {stats['failed']}")
        yield send_message("log", level="info", message=f"   耗时: {minutes}分{seconds}秒")
        yield send_message("log", level="info", message="─" * 40)
        if bucket_counter:
            yield send_message("log", level="info", message="📁 新文章分类:")
            for bucket, count in sorted(bucket_counter.items()):
                yield send_message("log", level="info", message=f"   {bucket}: {count} 篇")
            yield send_message("log", level="info", message="─" * 40)
        yield send_message("log", level="success", message="✅ 导出完成!")
        yield send_message("log", level="info", message=f"📂 输出目录: {config.output_dir}")

        # Send complete message with summary only (NOT full article content!)
        # The frontend already received article summaries via 'result' messages
        yield send_message(
            "complete",
            data={
                "exportedAt": job["result"]["exportedAt"],
                "totalUnique": total_articles,
                "newArticles": len(full_articles),
                "skipped": skipped_count,
                "stats": stats,
                "bucketCounter": bucket_counter,
                "duration": job["result"]["duration"],
            },
            message=f"导出完成! 新增 {len(full_articles)} 篇文章",
        )

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"] = str(e)
        yield send_message("log", level="error", message=f"❌ 导出失败: {e}")
        yield send_message("log", level="error", message=traceback.format_exc())
        yield send_message("error", message=f"导出失败: {e}")


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