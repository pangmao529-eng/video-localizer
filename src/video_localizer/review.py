"""A local, shareable index of outputs and time-coded review intervals."""

from html import escape
from pathlib import Path
from urllib.parse import quote


LABELS = {
    "completed": "已生成", "needs_review": "有画面待复核", "no_text_detected": "未识别到需替换的文字",
    "no_change_needed": "沿用原文", "failed": "处理失败", "not_processed": "尚未处理",
}


def write_review(folder, entries):
    def html(value):
        return escape(str(value), quote=True)

    sections = []
    for row in entries:
        title = html(Path(row["source"]).name)
        status = html(LABELS.get(row["status"], row["status"]))
        heading = f"<h2>{title}</h2><p>{status}</p>"
        if not row.get("output"):
            sections.append(heading + "<p>" + html(row.get("message") or row.get("reason", "")) + "</p>")
            continue
        video = quote(Path(row["output"]).name)
        parts = [heading, f'<video controls preload="none" src="{video}"></video>']
        issues = row.get("issues", [])
        if issues:
            parts.append("<ul>")
            for issue in issues:
                start, end = (max(0, float(issue.get(key, 0))) / 1000 for key in ("start_ms", "end_ms"))
                reason = issue.get("reason") or issue.get("code", "")
                text = html(issue.get("source_text", ""))
                parts.append(f'<li><a href="{video}#t={start:.3f},{end:.3f}">{start:.2f}–{end:.2f} 秒</a> {text} {html(reason)}</li>')
            parts.append("</ul>")
        sections.append("".join(parts))
    path = folder / "审核.html"
    path.write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><title>本批成片审核</title>'
                    '<style>body{font:16px system-ui;max-width:900px;margin:3rem auto;padding:0 1rem}'
                    'video{max-width:100%;max-height:55vh}li{margin:.5em 0}</style>'
                    '<h1>本批成片审核</h1>' + "".join(sections) + '</html>', encoding="utf-8")
    return str(path)
