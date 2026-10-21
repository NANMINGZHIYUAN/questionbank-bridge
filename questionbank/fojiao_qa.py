"""Official Fojiao AI-import JSON, generated locally from reviewed Q&A."""
from __future__ import annotations

import json
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from .core import strip_personal_answers, validate_bank
from .exporters import _answers, _source_paths, _type
from .manual_text import _pandoc, render_plain
from .word_export import CONTENT_CODES


def _brief(question):
    value = question.get("briefExplanation", "")
    if value.strip():
        return value
    blocks = []
    for value in (question.get("originalExplanation", ""), question.get("explanation", "")):
        if value.strip() and value not in blocks:
            blocks.append(value)
    return "\n\n".join(blocks)


class _HtmlImages(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.sources.append(dict(attrs).get("src") or "")


def _inline_images(value, pandoc):
    if not re.search(r"!\[|<img\b", value, re.I):
        return []
    # Use the existing parser to distinguish real images from escaped syntax,
    # code and math. Resolve reference-style Markdown images as well.
    source, _ = _pandoc(pandoc, value, "markdown+tex_math_dollars+tex_math_single_backslash", "json")
    sources = []

    def walk(node):
        if isinstance(node, dict):
            kind, content = node.get("t"), node.get("c")
            if kind in {"Code", "CodeBlock", "Math"}:
                return
            if kind == "Image":
                sources.append(content[2][0])
                return
            if kind in {"RawInline", "RawBlock"}:
                if content[0] == "html":
                    parser = _HtmlImages()
                    parser.feed(content[1])
                    sources.extend(parser.sources)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(json.loads(source))
    return sources


def _media_identities(value, bases):
    value = unescape(str(value)).strip()
    if not value:
        return set()
    parsed = urlsplit(value)
    if parsed.scheme.lower() in {"http", "https"}:
        return {("url", parsed.scheme.lower(), parsed.netloc.lower(),
                 unquote(parsed.path), parsed.query)}
    if parsed.scheme.lower() == "file":
        value = ("//" + parsed.netloc if parsed.netloc else "") + unquote(parsed.path)
        if re.match(r"^/[a-z]:", value, re.I):
            value = value[1:]
    elif not re.match(r"^[a-z]:", value, re.I):
        value = unquote(parsed.path)
    path = Path(value)
    candidates = [path] if path.is_absolute() else [base / path for base in bases]
    return {("path", str(candidate.resolve()).casefold()) for candidate in candidates}


def export_qa_json(bank, out, pandoc, report):
    """Write only the official four fields; IDs and checks stay in the local report.

    This is an additional import channel, so existing Word/manual route counts
    remain unchanged. The platform's JSON text size limit is not established.
    """
    working = strip_personal_answers(bank)
    diagnostics = validate_bank(working)
    questions = [q for q in working["questions"] if _type(q) == "简答"]
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    report.setdefault("files", {}).pop("fojiaoQaText", None)
    report.setdefault("warnings", [])
    qa = report["fojiaoQaExport"] = {
        "selectedQuestions": len(questions), "exportedQuestions": 0, "deferredQuestions": 0,
        "selectedQuestionIds": [q["id"] for q in questions], "exportedQuestionIds": [],
        "deferredQuestionIds": [], "caseInStemIds": [], "deferred": [], "warnings": [],
        "parts": [], "platformImportVerified": False, "platformTextLimitVerified": False,
        "entry": "批量导入→AI导题", "localAiCalls": 0, "newDependencies": 0,
    }
    content_reasons = {}
    placement_ids = {d.get("questionId") for d in diagnostics if d["code"] == "invalid_image_placement"}
    optional_placement = {q["id"] for q in questions
                          if q["images"] and not any(image["required"] for image in q["images"])
                          and q["id"] in placement_ids}
    for diagnostic in diagnostics:
        if diagnostic["code"] in CONTENT_CODES and not (
                diagnostic.get("questionId") in optional_placement and diagnostic["code"] == "invalid_image_placement"):
            content_reasons.setdefault(diagnostic.get("questionId"), []).append(diagnostic["code"])
    word = report.get("wordExport", {})
    detailed_pending = set()
    for item in word.get("pending", []):
        qid = item["questionId"]
        detailed_pending.add(qid)
        ignored = {"invalid_image_placement", "选项图片的位置索引无效，需回看原题。", "题图位置字段无效，需回看原题。"}
        content_reasons.setdefault(qid, []).extend(
            reason for reason in item["reasons"] if not (qid in optional_placement and reason in ignored))
    for qid in word.get("pendingQuestionIds", []):
        if not content_reasons.get(qid) and qid not in detailed_pending:
            content_reasons[qid] = ["content_pending"]

    def warn(qid, field, message):
        entry = {"questionId": qid, "field": field, "message": message}
        if entry not in qa["warnings"]:
            qa["warnings"].append(entry)
        line = f"问答 JSON {qid} {field}：{message}"
        if line not in report["warnings"]:
            report["warnings"].append(line)

    def plain(qid, field, value):
        text, warnings = render_plain(value, pandoc)
        for message in warnings:
            warn(qid, field, message)
        return text

    records = []
    for question in questions:
        qid = question["id"]
        reasons = list(content_reasons.get(qid, []))
        images = question.get("images", [])
        if any(image["required"] for image in images):
            reasons.append("required_media_not_supported_by_json")
        if qid in optional_placement:
            warn(qid, "附件", "非关键图片的定位登记需核对；可靠文字可进入 JSON，原附件及 Word 分类保留。")
        answer = "\n\n".join(_answers(question))
        fields = [("案例", question.get("case", ""), "title"),
                  ("题干", question["title"], "title"), ("答案", answer, "answer"),
                  ("解析", _brief(question), "explanation")]
        fields += [("题干", option["text"], "option") for option in question.get("options", [])]
        bases = _source_paths(working, question) + [Path.cwd()]
        for label, value, location in fields:
            try:
                references = _inline_images(value, pandoc)
                for reference in references:
                    identities = _media_identities(reference, bases)
                    matches = [image for image in images if any(
                        identities & _media_identities(image.get(key, ""), bases) for key in ("path", "url"))]
                    if not matches:
                        reasons.append("undeclared_inline_media")
                        warn(qid, label, "内联图片没有匹配来源的非关键媒体登记；需核对原图及位置，保留完整本地材料。")
                    elif any("field" not in image or image["field"] != location for image in matches):
                        warn(qid, label, "非关键图片来源已匹配，旧位置未登记或与当前字段不同；按原件核对附件位置。")
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
                reasons.append("inline_media_needs_review")
                warn(qid, label, "内联媒体引用未能可靠核对；保留完整原稿并单独核验该题。")
        if reasons:
            qa["deferredQuestionIds"].append(qid)
            qa["deferred"].append({"questionId": qid, "reasons": sorted(set(reasons))})
            continue
        if images:
            warn(qid, "附件", "非关键图片仅保留在本地附件和备用稿；此 JSON 通道只导入可靠文字，图片显示尚未验证。")
        case = plain(qid, "案例", question.get("case", ""))
        stem = plain(qid, "题干", question["title"])
        if question.get("options"):
            # Retain unusual Q&A option text without reclassifying the question.
            options = []
            for option in question["options"]:
                text = plain(qid, "题干", option["text"])
                if text:
                    label = plain(qid, "题干", option["label"])
                    options.append((label + ". " if label else "") + text)
            if options:
                stem += "\n\n" + "\n".join(options)
        answer = plain(qid, "答案", answer)
        explanation = plain(qid, "解析", _brief(question))
        if not stem.strip() or not answer.strip():
            qa["deferredQuestionIds"].append(qid)
            qa["deferred"].append({"questionId": qid, "reasons": ["empty_upload_field_after_cleanup"]})
            continue
        if case:
            stem = "案例：" + case + "\n\n" + stem
            qa["caseInStemIds"].append(qid)
        records.append({"题型": "问答题", "题干": stem, "答案": answer, "解析": explanation})
        qa["exportedQuestionIds"].append(qid)

    qa["exportedQuestions"] = len(records)
    qa["deferredQuestions"] = len(qa["deferredQuestionIds"])
    selected = set(qa["selectedQuestionIds"])
    exported, deferred = set(qa["exportedQuestionIds"]), set(qa["deferredQuestionIds"])
    if exported & deferred or exported | deferred != selected:
        raise RuntimeError("问答 JSON 导出未满足 ID 集合守恒。")
    if records:
        target = out / "佛脚问答批量导入.txt"
        payload = json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        target.write_text(payload, encoding="utf-8", newline="\n")
        saved = target.read_text(encoding="utf-8")
        if json.loads(saved) != records:
            raise RuntimeError("问答 JSON 写入后内容核对失败。")
        report["files"]["fojiaoQaText"] = str(target)
        qa["parts"].append({"file": str(target), "bytes": target.stat().st_size,
                            "characters": len(saved), "questions": [
                                {"number": number, "questionId": qid}
                                for number, qid in enumerate(qa["exportedQuestionIds"], 1)]})
