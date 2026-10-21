"""Export a reviewed, platform-independent bank without inventing knowledge."""
from __future__ import annotations

import copy
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from .core import TYPE_ALIASES, UNSAFE_CARD_CODES, strip_personal_answers, validate_bank
from .intake import strip_personal_answer_text


_LEARNING_URL = re.compile(
    r"https?://[^\s<>\]\)\"']*(?:chaoxing\.com|xueyinonline\.com)[^\s<>\]\)\"']*",
    re.IGNORECASE,
)


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value)).strip(" .")
    if not value:
        value = "未命名课程"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", value):
        value = "_" + value
    return value[:100].rstrip(" .")


class _ReadableHTML(HTMLParser):
    """Keep visible text, never scripts, styles, HTML attributes or image URLs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object"}:
            self.blocked += 1
        if self.blocked:
            return
        if tag in {"p", "div", "li", "br", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")
        elif tag == "img":
            self.parts.append("[图片]")
        elif tag == "sup":
            self.parts.append("^")
        elif tag == "sub":
            self.parts.append("_")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object"}:
            self.blocked = max(0, self.blocked - 1)
        elif not self.blocked and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(data)


def _text(value) -> str:
    text = str(value or "")
    # Literal markup inside code examples is data, not HTML to remove.
    pieces = re.split(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", text, flags=re.S)
    for index in range(0, len(pieces), 2):
        if re.search(r"</?[A-Za-z][^>]*>", pieces[index]):
            parser = _ReadableHTML()
            parser.feed(pieces[index])
            pieces[index] = "".join(parser.parts)
    text = "".join(pieces)
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _math_markdown(text: str) -> str:
    pieces = re.split(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", text, flags=re.S)
    for index in range(0, len(pieces), 2):
        piece = re.sub(r"\\\[(.*?)\\\]", lambda m: "\n\n$$\n" + m[1].strip() + "\n$$\n\n", pieces[index], flags=re.S)
        pieces[index] = re.sub(r"\\\((.*?)\\\)", lambda m: "$" + m[1].strip() + "$", piece, flags=re.S)
    return "".join(pieces)


def _web_reference(destination) -> str | None:
    value = html.unescape(str(destination or "").strip())
    if value.startswith("<") and ">" in value:
        end = value.index(">")
        suffix, value = value[end + 1:].strip(), value[1:end]
    else:
        fields = value.split(None, 1)
        value = fields[0] if fields else ""
        suffix = fields[1].strip() if len(fields) > 1 else ""
    if suffix and not re.fullmatch(r'"[^"\n]*"|\x27[^\x27\n]*\x27', suffix):
        return None
    value = value.replace(r"\(", "(").replace(r"\)", ")")
    decoded = unquote(unquote(value))
    if re.search(r'[\s<>"`\\\x00-\x1f\x7f]', value) or re.search(r'[\\\x00-\x1f\x7f]', decoded):
        return None
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return None
        if parsed.username is not None or parsed.password is not None or "%" in parsed.netloc:
            return None
        parsed.port  # Validate malformed/out-of-range ports before emitting href.
    except ValueError:
        return None
    if any(hostname == domain or hostname.endswith("." + domain) for domain in ("chaoxing.com", "xueyinonline.com")) or _LEARNING_URL.search(decoded):
        return None
    return quote(value, safe="/:?#[]@!$&'*+,;=%")


def _inline_link_parts(text):
    """Yield text and inline links, allowing balanced parentheses in URLs."""
    position = 0
    pattern = re.compile(r"(!?)\[([^\n]+?)\]\(")
    while match := pattern.search(text, position):
        end, depth = match.end(), 1
        while end < len(text) and depth:
            if text[end] == "\\" and end + 1 < len(text):
                end += 2
                continue
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
            end += 1
        if depth:
            break
        yield "text", text[position:match.start()], None
        yield "image" if match[1] else "link", match[2], _web_reference(text[match.end():end - 1])
        position = end
    yield "text", text[position:], None


def _reference_markdown(piece):
    rendered = []
    for kind, label, url in _inline_link_parts(piece):
        if kind == "image":
            rendered.append("[图片：" + label + "]")
        elif kind == "link" and url:
            rendered.append(_link(label, url))
        else:
            rendered.append(label)
    text = "".join(rendered)
    # Reference definitions can otherwise bypass inline destination checks.
    def reference_definition(match):
        url = _web_reference(match[2])
        return f"[{match[1]}]: {url}" if url else match[1]
    return re.sub(r"(?m)^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(.+)$", reference_definition, text)


def _markdown(value, heading_base: int = 0) -> str:
    text = _math_markdown(_text(value))
    # Preserve public web citations. Generated internal links are added separately.
    pieces = re.split(r"(```.*?```|~~~.*?~~~|`[^`\n]+`|\$\$.*?\$\$|(?<!\$)\$[^$\n]+\$(?!\$))", text, flags=re.S)
    for index in range(0, len(pieces), 2):
        piece = _reference_markdown(pieces[index])
        piece = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m[2] or m[1], piece)
        if heading_base:
            piece = re.sub(r"(?m)^(#{1,6})[ \t]+", lambda m: "#" * min(6, len(m[1]) + heading_base) + " ", piece)
        pieces[index] = piece
    text = "".join(pieces)
    text = _LEARNING_URL.sub("[来源链接保留在统一题库.json]", text)
    return text


def _inline(value) -> str:
    return _markdown(value).replace("\n", " ").replace("|", "\\|")


def _link(label, target) -> str:
    label = str(label).replace("[", r"\[").replace("]", r"\]")
    return f"[{label}]({quote(str(target), safe='/:?#[]@!$&*+,;=%')})"


def _anki_html(value, citations=False) -> str:
    text = _math_markdown(_text(value))
    text = _LEARNING_URL.sub("[来源链接已省略]", text)
    # Escaping precedes adding our own HTML. Exported options cannot become markup.
    parts = []
    pieces = re.split(r"(```.*?```|~~~.*?~~~|`[^`\n]+`|\$\$.*?\$\$|(?<!\$)\$[^$\n]+\$(?!\$))", text, flags=re.S)
    for index, piece in enumerate(pieces):
        if index % 2 and piece.startswith("$"):
            display = piece.startswith("$$")
            delimiter = 2 if display else 1
            opening, closing = (r"\[", r"\]") if display else (r"\(", r"\)")
            parts.append(opening + html.escape(piece[delimiter:-delimiter].strip()) + closing)
        elif index % 2:
            parts.append(html.escape(piece).replace("\n", "<br>"))
        else:
            for kind, label, url in _inline_link_parts(piece):
                escaped = html.escape(label).replace("\n", "<br>")
                if kind == "link" and url and citations:
                    parts.append(f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{escaped}</a>')
                elif kind == "image":
                    parts.append("[图片：" + escaped + "]")
                else:
                    parts.append(escaped)
    return "".join(parts)


def _type(question) -> str:
    value = question.get("type", "未知")
    return TYPE_ALIASES.get(value, value)


def _comparison(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", _text(value)).split())


def _answer_option(answer, options):
    normalized = _comparison(answer)
    matches = [option for option in options if _comparison(option.get("text", "")) == normalized]
    if matches:
        return matches[0] if len(matches) == 1 else None
    labels = [option for option in options if _comparison(option.get("label", "")).upper() == normalized.upper()]
    if len(labels) == 1:
        return labels[0]
    return None


def _answers(question) -> list[str]:
    values = question.get("correctAnswer") or []
    if not isinstance(values, list):
        values = [values]
    return [str(value).strip() for value in values if str(value).strip()]


def _unsafe_reason(question, required_missing=False) -> str | None:
    issue_codes = {issue.get("code", "") for issue in question.get("issues", [])}
    found = sorted(issue_codes & UNSAFE_CARD_CODES)
    if found:
        return ", ".join(found)
    kind = _type(question)
    if kind not in {"单选", "多选", "不定项", "判断", "填空", "简答"}:
        return "unknown_type"
    if not _text(question.get("title")):
        return "empty_title"
    answers = _answers(question)
    if not answers:
        return "missing_correct_answer"
    if required_missing:
        return "missing_required_image"
    if kind in {"单选", "多选", "不定项"}:
        options = question.get("options", [])
        labels = [str(item.get("label", "")).upper().strip() for item in options]
        if not labels or len(set(labels)) != len(labels):
            return "invalid_answer"
        if kind == "单选" and len(answers) != 1:
            return "invalid_answer"
        if any(_answer_option(answer, options) is None for answer in answers):
            return "invalid_answer"
    elif kind == "判断":
        valid = {"正确", "错误", "对", "错", "√", "×", "是", "否", "true", "false", "t", "f", "1", "0", "y", "n"}
        valid.update(str(item.get("label", "")).lower() for item in question.get("options", []))
        if len(answers) != 1 or answers[0].lower() not in valid:
            return "invalid_answer"
    return None


def _answer_text(question) -> str:
    options = question.get("options", [])
    answer = _answers(question)
    if _type(question) in {"单选", "多选", "不定项"}:
        values = []
        for value in answer:
            option = _answer_option(value, options)
            values.append(f"{option['label']}. {_text(option['text'])}" if option else value)
        return "\n".join(values)
    return "\n".join(answer)


def _source_paths(bank, question) -> list[Path]:
    wanted = {item.get("sourceId") for item in question.get("sources", [])}
    candidates = []
    for source in bank.get("sources", []):
        if not wanted or source.get("id") in wanted:
            if source.get("path"):
                candidates.append(Path(source["path"]).parent)
    return candidates


def _prepare_media(bank, out, report):
    media = {}
    missing_required = set()
    files = {}
    for question in bank.get("questions", []):
        qid = str(question.get("id", ""))
        media[qid] = []
        for item in question.get("images", []):
            path = item.get("path")
            resolved = None
            if path and not re.match(r"(?i)https?://", str(path)):
                candidate = Path(path)
                candidates = [candidate] if candidate.is_absolute() else [base / candidate for base in _source_paths(bank, question)] + [candidate]
                resolved = next((p for p in candidates if p.is_file()), None)
            if resolved is None:
                required = bool(item.get("required", False))
                report["missingMedia"].append({"questionId": qid, "required": required, "reason": "not_local_or_missing"})
                if required:
                    missing_required.add(qid)
                # A missing path should not pretend to point to an exported asset.
                if path:
                    item["originalPath"] = path
                    item.pop("path", None)
                continue
            content_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
            suffix = resolved.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                suffix = ".bin"
            filename = content_hash + suffix
            target = out / "assets" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(resolved, target)
            item["path"] = "assets/" + filename
            media[qid].append(filename)
            files[filename] = target
    return media, missing_required, files


def _related_links(question_ids, qrefs, prefix="") -> list[str]:
    return [_link(f"{qrefs[qid]} / {_inline(qid)}", f"{prefix}题目索引.md#{qrefs[qid]}") for qid in question_ids if qid in qrefs]


def _card_reason(card, topic, qids, skipped):
    references = [str(qid) for qid in (card.get("questionIds") or topic.get("questionIds") or [])]
    if not _text(card.get("prompt")) or not _text(card.get("answer")):
        return "empty_card", references
    if not references or any(qid not in qids for qid in references):
        return "unknown_question_reference", references
    if any(qid in skipped for qid in references):
        return "unsafe_question_reference", references
    return None, references


def _public_source_id(value):
    value = str(value)
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else "src-" + _digest(value)


def _public_locator(value):
    locations = []
    for part in re.split(r"[;；]", str(value or "")):
        part = part.strip()
        if re.fullmatch(r"(?:items|questions)\[\d{1,8}\]", part):
            locations.append(part)
        elif re.fullmatch(r"id\s*[:=]\s*[A-Za-z0-9_-]{1,64}", part, re.I):
            locations.append(part)
        elif re.fullmatch(r"(?:pages?|lines?)\s*[:=]\s*\d{1,8}(?:\s*[,，-]\s*\d{1,8})*", part, re.I):
            locations.append(part)
    return "；".join(locations) or "定位信息保留在本地数据"


def _clean_raw_text(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "rawText" and isinstance(item, str):
                value[key] = strip_personal_answer_text(item)
            else:
                _clean_raw_text(item)
    elif isinstance(value, list):
        for item in value:
            _clean_raw_text(item)


def _topic_body(topic, qrefs, skipped, index_prefix="", heading_level=2) -> str:
    explanation = _markdown(topic.get("explanation", ""), heading_base=2)
    heading = "#" * heading_level
    lines = [heading + " 知识讲解", "", explanation or "未提供本主题的知识讲解正文。", "", heading + " 对应原题", ""]
    links = _related_links(topic.get("questionIds", []), qrefs, index_prefix)
    lines += ["- " + link for link in links] or ["未关联到本次导出的原题。"]
    cards = topic.get("cards", [])
    if cards:
        lines += ["", heading + " 考点问答", ""]
        for card in cards:
            reason, _ = _card_reason(card, topic, qrefs, skipped)
            lines += [f"**问：** {_markdown(card.get('prompt', ''))}", ""]
            if reason:
                lines += ["关联原题或问答内容待核对，未展示为确定答案问答。原整理内容保留在完整题库 JSON。", ""]
            else:
                lines += [f"**答：** {_markdown(card.get('answer', ''))}", ""]
    return "\n".join(lines)


def _write_markdown(bank, out, media, skipped, report):
    course = _inline(bank.get("course", "未命名课程"))
    name = _name(bank.get("course", "未命名课程"))
    knowledge = out / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    qrefs = {str(q.get("id", "")): f"Q{i:03d}" for i, q in enumerate(bank.get("questions", []), 1)}
    topics = bank.get("topics", [])
    topic_files = {str(topic.get("id", "")): f"{i:02d}-{_name(topic.get('title', '未命名主题'))}.md" for i, topic in enumerate(topics, 1)}
    map_name = f"00-{name}课程知识地图.md"
    map_lines = [f"# 00 {course}课程知识地图", "", _link("题目索引", "题目索引.md"), "", _link("来源索引", "来源索引.md"), "", "## 课程纲要", ""]
    full_knowledge = [f"# {course}知识讲解", "", _link("课程知识地图", f"knowledge/{map_name}"), ""]
    if not topics:
        message = "未提供课程主题和知识讲解。本次只导出原题，不自动生成知识或考点卡。"
        map_lines += [message]
        full_knowledge += [message]
    for i, topic in enumerate(topics, 1):
        tid = str(topic.get("id", ""))
        title = _inline(topic.get("title", "未命名主题"))
        filename = topic_files[tid]
        summary = _inline(topic.get("summary", ""))
        map_lines += ["- " + _link(f"{i:02d} {title}", filename) + (f"：{summary}" if summary else "")]
        note = [f"# {i:02d} {title}", "", _link("返回课程知识地图", map_name), "", _topic_body(topic, qrefs, skipped)]
        (knowledge / filename).write_text("\n".join(note).rstrip() + "\n", encoding="utf-8")
        full_knowledge += [f"## {i:02d} {title}", "", _topic_body(topic, qrefs, skipped, "knowledge/", heading_level=3), ""]
    (knowledge / map_name).write_text("\n".join(map_lines).rstrip() + "\n", encoding="utf-8")
    source_lines = ["# 来源索引", "", _link("返回课程知识地图", map_name), "", "本页登记来源 ID、格式和关联题数；精确来源路径与网址保留在本地数据。", "", "| 来源 ID | 格式 | 关联题数 |", "| --- | --- | --- |"]
    for source in bank.get("sources", []):
        count = sum(any(ref.get("sourceId") == source["id"] for ref in question.get("sources", [])) for question in bank.get("questions", []))
        format_name = source.get("format", "")
        if not re.fullmatch(r"[A-Za-z0-9 ._+-]{1,40}", format_name):
            format_name = "未标明格式"
        source_lines.append(f"| {_public_source_id(source['id'])} | {format_name} | {count} |")
    (knowledge / "来源索引.md").write_text("\n".join(source_lines).rstrip() + "\n", encoding="utf-8")
    index = ["# 题目索引", "", _link("返回课程知识地图", map_name), "", _link("来源索引", "来源索引.md"), ""]
    questions_doc = [f"# {course}题库", "", f"共 {len(bank.get('questions', []))} 题。原始答案与来源原值保留在{name}题库.json。", ""]
    for question in bank.get("questions", []):
        qid = str(question.get("id", ""))
        ref = qrefs[qid]
        linked_topics = [_link(_inline(next(t.get('title', '') for t in topics if str(t.get('id', '')) == str(tid))), topic_files[str(tid)]) for tid in question.get("topicIds", []) if str(tid) in topic_files]
        if not linked_topics:
            linked_topics = [_link(_inline(t.get('title', '')), topic_files[str(t.get('id', ''))]) for t in topics if qid in t.get("questionIds", [])]
        index += [f"## {ref}", "", f"**原题 ID：** `{qid}`", "", _markdown(question.get("title", "")), "", "**知识页：** " + ("；".join(linked_topics) or "未关联知识页"), "", _link("完整原题", f"../{name}题库.md#{ref}"), ""]
        sources = [f"{_link(_public_source_id(source.get('sourceId', '')), '来源索引.md')}（{_public_locator(source.get('locator'))}）" for source in question.get("sources", [])]
        index += ["**来源定位：** " + ("；".join(sources) or "尚未登记来源"), ""]
        questions_doc += [f"## {ref}", "", f"**原题 ID：** `{qid}`　**题型：** {_type(question)}", "", _markdown(question.get("title", "")), ""]
        if question.get("case"):
            questions_doc += ["**案例：**", "", _markdown(question['case']), ""]
        for option in question.get("options", []):
            questions_doc += [f"- {_inline(option.get('label', ''))}. {_markdown(option.get('text', ''))}"]
        for filename in media.get(qid, []):
            questions_doc += ["", f"![{ref} 题图](assets/{filename})"]
        questions_doc += ["", "**原题参考答案：**", "", _markdown(_answer_text(question)) or "未提供确定答案。", ""]
        if question.get("variants"):
            questions_doc += ["**存在答案冲突，保留各来源版本：**", ""]
            for variant in question["variants"]:
                answer = variant.get("correctAnswer", [])
                questions_doc += ["- " + _inline("；".join(map(str, answer)) if isinstance(answer, list) else answer)]
            questions_doc += [""]
        if question.get("originalExplanation"):
            questions_doc += ["**原题解析：**", "", _markdown(question["originalExplanation"]), ""]
        if question.get("explanation"):
            questions_doc += ["**知识讲解：**", "", _markdown(question["explanation"]), ""]
        doc_topic_links = [_link(t.get('title', ''), 'knowledge/' + topic_files[str(t['id'])])
                           for t in topics if str(t['id']) in question.get('topicIds', [])
                           or qid in t.get('questionIds', [])]
        questions_doc += ["**关联知识点：** " + ("；".join(doc_topic_links) or "未关联知识点"), ""]
        if qid in skipped:
            questions_doc += [f"**复习提示：** 本题未生成确定答案卡（{skipped[qid]}）。", ""]
        if question.get("issues"):
            questions_doc += ["**数据提示：** " + "；".join(_inline(issue.get("message") or issue.get("code", "")) for issue in question["issues"]), ""]
    (knowledge / "题目索引.md").write_text("\n".join(index).rstrip() + "\n", encoding="utf-8")
    (out / "题库.md").write_text("\n".join(questions_doc).rstrip() + "\n", encoding="utf-8")
    (out / "知识讲解.md").write_text("\n".join(full_knowledge).rstrip() + "\n", encoding="utf-8")
    report["files"].update({
        "questionMarkdown": str(out / "题库.md"), "knowledgeMarkdown": str(out / "知识讲解.md"),
        "knowledgeMap": str(knowledge / map_name), "questionIndex": str(knowledge / "题目索引.md"),
        "sourceIndex": str(knowledge / "来源索引.md"),
        "topicPages": [str(knowledge / topic_files[str(t.get("id", ""))]) for t in topics],
    })


def _reference_document(path):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    document = Document()
    for section in document.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.top_margin = section.bottom_margin = Cm(2)
        section.left_margin = section.right_margin = Cm(2.2)
    specifications = [("Normal", 12), ("Title", 22)] + [(f"Heading {level}", {1: 20, 2: 16, 3: 14}.get(level, 12)) for level in range(1, 10)]
    for name, size in specifications:
        style = document.styles[name]
        style.font.name = "Microsoft YaHei"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.insert(0, fonts)
        for field in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn("w:" + field), "Microsoft YaHei")
        paragraph = style.paragraph_format
        paragraph.line_spacing = 1.4
        paragraph.space_after = Pt(8)
        if name != "Normal":
            paragraph.keep_with_next = True
            paragraph.space_before = Pt(14)
    document.save(path)


def _word_documents(bank, out, pandoc, report, media, required_missing, word_qa_mode="manual"):
    from .word_export import export_word
    export_word(bank, out, pandoc, report, media, required_missing, word_qa_mode)


def _int_id(value):
    return (1 << 30) + int(_digest(value, 8), 16) % (1 << 30)


def _anki(bank, out, media, media_files, skipped, report, genanki):
    course = _text(bank.get("course", "未命名课程")).replace("::", "：")
    css = ".card{font-family:Arial,'Microsoft YaHei',sans-serif;font-size:20px;text-align:left;line-height:1.5;color:#111;background:#fff}.source{font-size:12px;color:#555}.answer{margin-top:12px}img{max-width:100%;height:auto}"
    original_model = genanki.Model(
        _int_id("questionbank:v1:original:model"), "题库工作流 · 原题卡 v1",
        fields=[{"name": value} for value in ["Question", "Answer", "Explanation", "OriginalId"]],
        templates=[{"name": "原题卡", "qfmt": '<div class="source">课程题库复习</div>{{Question}}', "afmt": '{{FrontSide}}<hr id="answer"><div class="answer"><b>课程参考答案：</b>{{Answer}}</div><div>{{Explanation}}</div><div class="source">{{OriginalId}}</div>'}], css=css,
    )
    topic_model = genanki.Model(
        _int_id("questionbank:v1:topic:model"), "题库工作流 · 考点卡 v1",
        fields=[{"name": value} for value in ["Prompt", "Answer", "OriginalIds"]],
        templates=[{"name": "考点卡", "qfmt": "{{Prompt}}", "afmt": '{{FrontSide}}<hr id="answer"><div class="answer">{{Answer}}</div><div class="source">{{OriginalIds}}</div>'}], css=css,
    )
    original = genanki.Deck(_int_id("questionbank:v1:original:deck:" + course), course + "::原题卡")
    topic_deck = genanki.Deck(_int_id("questionbank:v1:topic:deck:" + course), course + "::考点卡")
    for question in bank.get("questions", []):
        qid = str(question.get("id", ""))
        if qid in skipped:
            continue
        prompt = _anki_html(question.get("title", ""))
        if question.get('case'):
            prompt = '<b>案例：</b><br>' + _anki_html(question['case']) + '<br><br>' + prompt
        options = question.get("options", [])
        if options:
            prompt += "<ul>" + "".join("<li>" + _anki_html(item.get("label", "")) + ". " + _anki_html(item.get("text", "")) + "</li>" for item in options) + "</ul>"
        prompt += "".join(f'<br><img src="{html.escape(filename, quote=True)}">' for filename in media.get(qid, []))
        explanations = []
        if question.get("originalExplanation"):
            explanations.append("原题解析：" + _anki_html(question["originalExplanation"]))
        if question.get("explanation"):
            explanations.append("知识讲解：" + _anki_html(question["explanation"], citations=True))
        original.add_note(genanki.Note(model=original_model, fields=[prompt, _anki_html(_answer_text(question)), "<br>".join(explanations), _anki_html("原题 ID：" + qid)], guid=genanki.guid_for("questionbank:v1:original", course, qid), tags=["questionbank", "原题卡"]))
    qids = {str(q.get("id", "")) for q in bank.get("questions", [])}
    for topic in bank.get("topics", []):
        for card in topic.get("cards", []):
            reason, references = _card_reason(card, topic, qids, skipped)
            if reason:
                report["skippedTopicCards"] += 1
                report["skippedCards"].append({"kind": "topic", "id": card.get("id", ""), "reason": reason})
                continue
            cid = str(card.get("id") or _digest(str(topic.get("id", "")) + str(card.get("prompt", ""))))
            topic_deck.add_note(genanki.Note(model=topic_model, fields=[_anki_html(card["prompt"]), _anki_html(card["answer"], citations=True), _anki_html("对应原题 ID：" + "、".join(references))], guid=genanki.guid_for("questionbank:v1:topic", course, str(topic.get("id", "")), cid), tags=["questionbank", "考点卡"]))
    package = genanki.Package([original, topic_deck])
    used = {filename for qid, filenames in media.items() if qid not in skipped for filename in filenames}
    package.media_files = [str(media_files[filename]) for filename in sorted(used)]
    target = out / (_name(bank.get("course", "未命名课程")) + ".apkg")
    package.write_to_file(str(target))
    report.update(originalCards=len(original.notes), topicCards=len(topic_deck.notes), mediaCount=len(used))
    report["files"]["anki"] = str(target)


def _course_filenames(out: Path, report: dict, course: str) -> dict:
    """Name registered deliverables by course before atomic publication.

    Internal reports and knowledge index filenames stay fixed. File contents
    are untouched; Markdown navigation is authored with its final target name.
    """
    out = Path(out).resolve()
    name = _name(course)
    suffixes = {
        "normalizedJson": "题库.json",
        "questionMarkdown": "题库.md",
        "knowledgeMarkdown": "知识讲解.md",
        "manualQuestions": "问答题手动录入.txt",
        "manualQuestionsMarkdown": "问答题手动录入.md",
        "fojiaoQaText": "佛脚问答批量导入.txt",
    }
    files = report.get("files", {})
    targets = [(files[key], name + suffix) for key, suffix in suffixes.items() if files.get(key)]
    word_paths = list(files.get("questionDocxParts", []))
    if files.get("questionDocx"):
        word_paths.append(files["questionDocx"])
    for value in word_paths:
        filename = Path(value).name
        part = re.fullmatch(r"题库(-\d+)?\.docx", filename)
        if part is None:
            # This also makes repeated calls on already named artifacts safe.
            part = re.fullmatch(re.escape(name) + r"题库(-\d+)?\.docx", filename)
        if part is None:
            raise ValueError("未识别的题库 Word 分包文件名：" + filename)
        targets.append((value, name + "题库" + (part.group(1) or "") + ".docx"))
    renames = {}
    paths = {}
    for value, filename in targets:
        source = Path(value).resolve()
        target = out / filename
        if source.parent != out or not source.is_file() or Path(value).is_symlink():
            raise ValueError("交付命名只能处理输出目录中已登记的普通文件：" + str(value))
        if source == target:
            continue
        if target.exists():
            raise ValueError("课程交付文件名已存在，未覆盖：" + filename)
        if target in paths.values() and paths.get(source) != target:
            raise ValueError("课程交付文件名重复：" + filename)
        paths[source] = target
        renames[str(value)] = str(target)
    # All targets have been checked before touching staged files.
    for source, target in paths.items():
        source.rename(target)
    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return renames.get(value, value) if isinstance(value, str) else value
    updated = rewrite(report)
    report.clear()
    report.update(updated)
    return report


def _generate_bundle(bank: dict, out: Path, pandoc: str = "pandoc", word_qa_mode: str = "manual") -> dict:
    """Write JSON, Markdown, Word and an Anki package; return verifiable counts.

    No answers or topics are generated. Missing/ambiguous questions stay in the
    documents but do not become definitive-answer flashcards. Original inputs are
    never changed and remote media is never fetched.
    """
    diagnostics = validate_bank(bank)
    out = Path(out).resolve()
    if out.exists() and not out.is_dir():
        raise ValueError("输出位置必须是目录，不能覆盖来源文件。")
    for source in bank.get("sources", []):
        if source.get("path") and Path(source["path"]).resolve().is_relative_to(out):
            raise ValueError("原始来源不能位于输出目录内；请使用独立的输出目录。")
    try:
        import genanki
        import docx  # noqa: F401; fail before emitting a partial bundle
    except ImportError as exc:
        raise RuntimeError("导出依赖未就绪：请安装 genanki 和 python-docx。") from exc
    if not (Path(pandoc).is_file() or shutil.which(str(pandoc))):
        raise RuntimeError(f"找不到 Pandoc：{pandoc}")
    out.mkdir(parents=True, exist_ok=True)
    working = strip_personal_answers(bank)
    _clean_raw_text(working)
    by_question = {question["id"]: question for question in working["questions"]}
    for diagnostic in diagnostics:
        if diagnostic.get("questionId") in by_question:
            issue = {"code": diagnostic["code"], "message": diagnostic["message"]}
            if issue not in by_question[diagnostic["questionId"]]["issues"]:
                by_question[diagnostic["questionId"]]["issues"].append(issue)
    report = {"questions": len(working.get("questions", [])), "topics": len(working.get("topics", [])), "originalCards": 0, "topicCards": 0, "skippedOriginalCards": 0, "skippedTopicCards": 0, "mediaCount": 0, "missingMedia": [], "skippedCards": [], "diagnostics": diagnostics, "warnings": [], "files": {}}
    media, required_missing, files = _prepare_media(working, out, report)
    for qid in sorted(required_missing):
        issue = {"code": "missing_required_image", "message": "关键题图没有可读本地副本；远程图片尚未保存，需核对原题。"}
        if issue not in by_question[qid]["issues"]:
            by_question[qid]["issues"].append(issue)
        report["diagnostics"].append(dict(issue, questionId=qid))
    skipped = {}
    for question in working.get("questions", []):
        qid = str(question.get("id", ""))
        reason = _unsafe_reason(question, qid in required_missing)
        if reason:
            skipped[qid] = reason
            report["skippedCards"].append({"kind": "original", "id": qid, "reason": reason})
    report["skippedOriginalCards"] = len(skipped)
    normalized = out / "统一题库.json"
    normalized.write_text(json.dumps(working, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["files"]["normalizedJson"] = str(normalized)
    _write_markdown(working, out, media, skipped, report)
    _word_documents(working, out, pandoc, report, media, required_missing, word_qa_mode)
    from .fojiao_qa import export_qa_json
    # Match inline image references before media paths become staged hash names.
    export_qa_json(bank, out, pandoc, report)
    _anki(working, out, media, files, skipped, report, genanki)
    return _course_filenames(out, report, bank.get("course", "未命名课程"))


def export_bundle(bank: dict, out: Path, pandoc: str = "pandoc", word_qa_mode: str = "manual") -> dict:
    """Generate a complete bundle before publishing; retain the old on failure."""
    from .publication import publish_export
    out = Path(out).resolve()
    if out.exists() and not out.is_dir():
        raise ValueError("输出位置必须是目录，不能覆盖来源文件。")
    for source in bank.get("sources", []):
        if source.get("path") and Path(source["path"]).resolve().is_relative_to(out):
            raise ValueError("原始来源不能位于输出目录内；请使用独立的输出目录。")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="export-stage-", dir=out.parent) as folder:
        stage = Path(folder)
        report = _generate_bundle(bank, stage, pandoc, word_qa_mode)
        return publish_export(stage, out, report)
