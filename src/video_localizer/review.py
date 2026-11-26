"""Local index of completed outputs, previews and unresolved human decisions."""

from html import escape
from pathlib import Path
from urllib.parse import quote


LABELS = {"completed": "制作流程完成（验收状态见下方）", "needs_review": "需要人工决定",
          "failed": "处理失败", "not_processed": "尚未处理", "processing": "正在处理",
          "paused_error": "模型调用错误，已暂停", "paused_budget": "达到本轮模型调用上限，已暂停",
          "cancelled": "已取消，可续跑", "ready": "可继续处理"}


def write_review(folder, entries):
    folder = Path(folder).resolve()
    html = lambda value: escape(str(value), quote=True)

    def link(value, image=False):
        if not value:
            return None
        try:
            path = Path(value)
            path = (path if path.is_absolute() else folder / path).resolve()
            if not path.is_file():
                return None
            if image and path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                return None
            return quote(path.relative_to(folder).as_posix())
        except (OSError, TypeError, ValueError):
            return None

    def evidence_images(issue):
        parts = []
        for evidence in issue.get("evidence_images") or []:
            if not isinstance(evidence, dict):
                continue
            url = link(evidence.get("path"), image=True)
            if not url:
                continue
            label = evidence.get("label") or "审核画面"
            metadata = []
            for title, key in (("原片帧号", "source_frame"), ("时间（毫秒）", "time_ms"),
                               ("原片裁剪区域（像素）", "crop_source_pixels")):
                if evidence.get(key) is not None:
                    metadata.append(f"{title}：{html(evidence[key])}")
            parts.append(f'<figure><a href="{url}"><img loading="lazy" src="{url}" alt="{html(label)}"></a>'
                         f'<figcaption>{html(label)}{"；" if metadata else ""}{"；".join(metadata)}</figcaption></figure>')
        return '<div class="evidence">' + "".join(parts) + '</div>' if parts else ""

    def material_rows(issue):
        rows = issue.get("material_rows") or []
        if not isinstance(rows, list):
            rows = [rows]
        if not rows:
            return ""
        columns = list(dict.fromkeys(str(key) for row in rows if isinstance(row, dict) for key in row))
        parts = ['<h4>原资料行</h4>']
        if columns:
            parts.append('<div class="table-scroll"><table><thead><tr>'
                         + ''.join(f'<th>{html(key)}</th>' for key in columns) + '</tr></thead><tbody>')
            for row in rows:
                if isinstance(row, dict):
                    parts.append('<tr>' + ''.join(f'<td>{html(row.get(key, ""))}</td>' for key in columns) + '</tr>')
            parts.append('</tbody></table></div>')
        for row in rows:
            if not isinstance(row, dict):
                parts.append(f'<p>{html(row)}</p>')
        return ''.join(parts)

    sections = []
    for row in entries:
        parts = [f'<section><h2>{html(row.get("index", ""))}. {html(Path(row["source"]).name)}</h2>',
                 f'<p>{html(LABELS.get(row["status"], row["status"]))}</p>']
        if row.get("message"):
            parts.append(f'<p>{html(row["message"])}</p>')
        final, preview = link(row.get("output")), link(row.get("preview"))
        shown = final or preview
        if shown:
            parts.append(f'<p>{"成片" if final else "预览（尚未交付）"}</p><video controls preload="none" src="{shown}"></video>')
        else:
            parts.append('<p>尚无可播放成片或预览。下面的审核项仍可处理。</p>')
        parts.append(f'<p>模型复核：{"已记录" if row.get("model_reviewed") else "未完成"}；人工整片验收：'
                     f'{"已确认" if row.get("human_approved") else "未确认"}。</p>')
        if row.get("summary"):
            parts.append(f'<p>{html(row["summary"])}</p>')
        if row.get("coverage"):
            parts.append(f'<p>模型报告的检查范围：{html(row["coverage"])}</p>')
        issues = row.get("issues") or []
        if issues:
            parts.append('<h3>审核事项</h3><ul>')
            for issue in issues:
                parts.append(f'<li><strong>{html(issue.get("id", ""))}</strong> — '
                             f'{"待确认" if issue.get("status") == "pending" else "已记录意见"}')
                for label, field in (("原因", "reason"), ("原文", "source_text"), ("建议译文", "proposed_text"),
                                     ("画面依据", "evidence"), ("建议处理", "recommendation")):
                    if issue.get(field):
                        parts.append(f'<p>{label}：{html(issue[field])}</p>')
                if issue.get("frames"):
                    parts.append(f'<p>原片帧号：{html(", ".join(str(frame) for frame in issue["frames"]))}</p>')
                parts.append(evidence_images(issue))
                parts.append(material_rows(issue))
                if issue.get("decision"):
                    decision = issue["decision"]
                    parts.append(f'<p>人工意见：{html(decision.get("action", ""))} {html(decision.get("note", ""))}</p>')
                    for label, field in (("确认原文", "source_text"), ("确认译文", "target_text"),
                                         ("开始帧（含）", "start_frame"), ("结束帧（不含）", "end_frame")):
                        if decision.get(field) is not None:
                            parts.append(f'<p>{label}：{html(decision[field])}</p>')
                parts.append('</li>')
            parts.append('</ul>')
        # Keep private resumable model conversations out of the review page.
        # review.json contains the public task result, not full prompt history.
        report = link(Path(row["work_dir"]) / "review.json") if row.get("work_dir") else None
        if report:
            parts.append(f'<p><a href="{report}">本条制作与审核记录</a></p>')
        parts.append('</section>')
        sections.append("".join(parts))
    path = folder / "审核.html"
    path.write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><title>本批视频审核</title>'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<style>body{font:16px system-ui;max-width:960px;margin:3rem auto;padding:0 1rem;line-height:1.6}'
                    'video{max-width:100%;max-height:55vh}li{margin:1em 0}section{border-top:1px solid #ccc;margin-top:2rem}'
                    'li p,td{white-space:pre-wrap}li p{margin:.25rem 0}.evidence{display:flex;flex-wrap:wrap;gap:1rem}'
                    'figure{margin:.5rem 0;max-width:100%}figure img{max-width:100%;max-height:60vh}figcaption{font-size:.9rem}'
                    '.table-scroll{overflow-x:auto}table{border-collapse:collapse}th,td{padding:.4rem;border:1px solid #ccc;text-align:left}'
                    '</style><h1>本批视频审核</h1>'
                    '<p>所选云端模型负责视频制作判断。技术检查与生成成功不代表视觉验收通过；请核对本条模型复核和人工验收记录。</p>'
                    + "".join(sections) + '</html>', encoding="utf-8")
    return str(path)
