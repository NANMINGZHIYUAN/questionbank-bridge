"""Loss-aware normalization and merging for offline question banks.

All public operations return new JSON-serializable objects and leave their inputs
untouched. Answers observed on the source page and supplementary explanations
are kept separately. Learner responses are ignored in derived question banks.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


TYPES = {"单选", "多选", "不定项", "判断", "填空", "简答", "未知"}
TYPE_ALIASES = {
    "single": "单选", "single_choice": "单选", "single-choice": "单选",
    "multiple": "多选", "multiple_choice": "多选", "multiple-choice": "多选",
    "true_false": "判断", "true-false": "判断", "judgment": "判断",
    "fill": "填空", "fill_blank": "填空", "fill-in": "填空",
    "short": "简答", "short_answer": "简答", "essay": "简答",
    "unknown": "未知", "单选题": "单选", "单项选择题": "单选",
    "多选题": "多选", "多项选择题": "多选", "判断题": "判断",
    "不定项题": "不定项", "不定项选择": "不定项", "不定项选择题": "不定项",
    "任选": "不定项", "任选题": "不定项", "任选选择题": "不定项",
    "填空题": "填空", "简答题": "简答", "问答": "简答", "问答题": "简答",
}
# Consumers must not publish a deterministic answer card for these questions.
UNSAFE_CARD_CODES = frozenset({
    "missing_correct_answer", "unmapped_answer", "ambiguous_answer",
    "conflicting_correct_answers", "unknown_type", "missing_required_image",
    "requires_visual_review", "requires_content_review", "ambiguous_question_boundary", "numbered_field_continuation",
    "duplicate_option_label", "empty_title", "duplicate_options", "invalid_answer_cardinality",
    "missing_source_reference", "unknown_topic_reference", "unknown_question_reference",
    "unsafe_conflict_card",
})
_DERIVED_CODES = {
    "missing_correct_answer", "conflicting_correct_answers", "unknown_type",
    "missing_required_image", "empty_title", "duplicate_options",
    "unmapped_answer", "ambiguous_answer", "invalid_answer_cardinality", "invalid_image_placement",
}
_NUMBER_PREFIX = re.compile(
    r"^\s*(?:第\s*\d+\s*题\s*[:：、.]?\s*|\d+\s*[、．]\s*|"
    r"\d+\.(?!\d)\s*|[（(]\d+[）)]\s+)"
)


def file_sha256(path: str | Path) -> str:
    """Return a file's SHA-256 without loading the entire file into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strip_personal_field_text(text: str) -> str:
    """Filter a single canonical body field without inventing question borders.

    Unlike source-page text, this field cannot contain a new question just
    because a learner's answer has numbered steps. Code examples remain data.
    """
    from .intake import _BRACKET_PERSONAL_FIELD, _FIELD, _personal_label, _strip_heading
    output = []
    personal = False
    fence = None
    for row in text.splitlines(keepends=True):
        body = row.rstrip("\r\n")
        ending = row[len(body):]
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", body)
        if not personal and fence_match:
            if fence is None:
                fence = fence_match.group(1)[0]
            elif fence == fence_match.group(1)[0]:
                fence = None
            output.append(row)
            continue
        if fence is not None and not personal:
            output.append(row)
            continue
        line = _strip_heading(body)
        marker = _FIELD.match(line)
        if (marker and _personal_label(marker.group("label"))) or _BRACKET_PERSONAL_FIELD.match(line):
            personal = True
            output.append(ending)
            continue
        if personal and marker:
            personal = False
        output.append(ending if personal else row)
    return "".join(output)


def strip_personal_answers(value: Any) -> Any:
    """Copy JSON without learner fields or explicitly marked learner body text.

    Unlabeled prose, source metadata, formulas and code examples stay intact.
    Source-page rawText uses intake's conservative question-boundary filter.
    """
    if isinstance(value, dict):
        body_fields = {"title", "case", "text", "originalExplanation", "explanation",
                       "briefExplanation", "originalAnswer", "analysis", "prompt", "answer", "summary"}
        result = {}
        for key, item in value.items():
            if key in {"selectedAnswer", "rawSelectedAnswer"}:
                continue
            if key == "rawText" and isinstance(item, str):
                from .intake import strip_personal_answer_text
                result[key] = strip_personal_answer_text(item)
            elif key in body_fields and isinstance(item, str):
                result[key] = _strip_personal_field_text(item)
            elif key in {"correctAnswer", "rawCorrectAnswer"} and isinstance(item, list):
                result[key] = []
                for answer in item:
                    cleaned = _strip_personal_field_text(answer) if isinstance(answer, str) else copy.deepcopy(answer)
                    if not isinstance(cleaned, str) or cleaned.strip():
                        result[key].append(cleaned)
            else:
                result[key] = strip_personal_answers(item)
        return result
    if isinstance(value, list):
        return [strip_personal_answers(item) for item in value]
    return copy.deepcopy(value)


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", html.unescape(value)).split())


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _question_id(course: str, question: dict) -> str:
    identity = {
        "course": _text(course), "type": question["type"],
        "title": _NUMBER_PREFIX.sub("", _text(question["title"]), count=1),
        "options": [{"label": _text(o["label"]).upper(), "text": _text(o["text"])}
                    for o in question["options"]],
    }
    return "q-" + _digest(identity)[:24]


def _issue(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _unique(values: list) -> list:
    seen: set[str] = set()
    result = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if key not in seen:
            seen.add(key)
            result.append(copy.deepcopy(value))
    return result


def _string(value: Any, name: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _answers(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    result = []
    for answer in value:
        if not isinstance(answer, (str, int, float)) or isinstance(answer, bool):
            raise ValueError(f"{name} entries must be text or numbers")
        text = str(answer)
        if text.strip():
            result.append(text)
    return result


def _refs(value: Any, name: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    refs = []
    for ref in value:
        if not isinstance(ref, dict) or not isinstance(ref.get("sourceId"), str):
            raise ValueError(f"{name} requires sourceId strings")
        refs.append({"sourceId": ref["sourceId"],
                     "locator": _string(ref.get("locator"), f"{name}.locator")})
    return _unique(refs)


def _options(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("options must be an array")
    options = []
    for index, option in enumerate(value):
        if not isinstance(option, dict):
            raise ValueError("each option must be an object")
        label = _string(option.get("label"), "option.label").strip()
        if not label:
            label = chr(65 + index)
        options.append({"label": label, "text": _string(option.get("text"), "option.text")})
    return options


def _images(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("images must be an array")
    images = []
    for image in value:
        if isinstance(image, str):
            key = "url" if re.match(r"^(?:https?://|data:|blob:)", image, re.I) else "path"
            images.append({key: image, "required": True})
        elif isinstance(image, dict):
            if not isinstance(image.get("required", True), bool):
                raise ValueError("image.required must be a boolean")
            entry = {"required": image.get("required", True)}
            for key in ("path", "url"):
                if key in image:
                    entry[key] = _string(image[key], f"image.{key}")
            if "alt" in image:
                entry["alt"] = _string(image["alt"], "image.alt")
            if "field" in image:
                field = image["field"]
                if not isinstance(field, str) or field not in {"title", "option", "answer", "explanation"}:
                    raise ValueError("image.field must be title, option, answer or explanation")
                entry["field"] = field
                if field == "option":
                    index = image.get("optionIndex")
                    if type(index) is not int or index < 0:
                        raise ValueError("option image.optionIndex must be a non-negative integer")
                    entry["optionIndex"] = index
                elif "optionIndex" in image:
                    raise ValueError("image.optionIndex is only valid for field=option")
            elif "optionIndex" in image:
                raise ValueError("image.optionIndex requires field=option")
            images.append(entry)
        else:
            raise ValueError("images entries must be strings or objects")
    return _unique(images)


def _map_answers(answers: list[str], options: list[dict], kind: str) -> tuple[list[str], list[dict]]:
    if kind not in {"单选", "多选", "不定项", "判断"} or not answers:
        return list(answers), []
    issues = []
    mapped = []
    labels: dict[str, list[dict]] = {}
    texts: dict[str, list[dict]] = {}
    for option in options:
        labels.setdefault(_text(option["label"]).upper(), []).append(option)
        texts.setdefault(_text(option["text"]), []).append(option)
    for answer in answers:
        normalized = _text(answer)
        candidates = labels.get(normalized.upper(), [])
        # Multi-choice tokens may be AC, A C, A/C, A、C, or A,C.
        if not candidates and kind in {"多选", "不定项"} and re.fullmatch(r"[A-Za-z\s,，、;/|]+", normalized):
            tokens = re.findall(r"[A-Za-z]", normalized.upper())
            if len(tokens) > 1 and all(len(labels.get(token, [])) == 1 for token in tokens):
                mapped.extend(labels[token][0]["text"] for token in tokens)
                continue
        if not candidates:
            candidates = texts.get(normalized, [])
        if kind == "判断" and not candidates:
            truths = {"true", "t", "y", "正确", "对", "√", "是", "1"}
            falsehoods = {"false", "f", "n", "错误", "错", "×", "否", "0"}
            family = truths if normalized.lower() in truths else falsehoods if normalized.lower() in falsehoods else set()
            if family:
                candidates = [o for o in options if _text(o["text"]).lower() in family]
                if not options:
                    mapped.append("正确" if family is truths else "错误")
                    continue
        if len(candidates) == 1:
            mapped.append(candidates[0]["text"])
        else:
            mapped.append(answer)
            code = "ambiguous_answer" if len(candidates) > 1 else "unmapped_answer"
            issues.append(_issue(code, "答案无法唯一对应选项，保留原值，需人工核对。"))
    return mapped, _unique(issues)


def _answer_key(answers: list[str], kind: str) -> tuple[str, ...]:
    values = [_text(answer) for answer in answers]
    return tuple(sorted(set(values))) if kind in {"多选", "不定项"} else tuple(values)


def _content_issues(question: dict) -> list[dict]:
    issues = []
    if not question["title"].strip():
        issues.append(_issue("empty_title", "题干为空，需回看原始材料。"))
    if question["type"] == "未知":
        issues.append(_issue("unknown_type", "题型未识别，需人工指定。"))
    variants = question.get("variants", [])
    if len(variants) > 1:
        issues.append(_issue("conflicting_correct_answers", "相同题目存在不同正确答案，已保留版本并暂停确定答案卡。"))
    elif not any(answer.strip() for answer in question["correctAnswer"]):
        issues.append(_issue("missing_correct_answer", "来源未提供正确答案，个人答案不作为正确答案。"))
    answers = question["correctAnswer"]
    if (question["type"] in {"单选", "判断"} and len(answers) > 1) or (
        question["type"] in {"多选", "不定项"} and len({_text(answer) for answer in answers}) != len(answers)
    ):
        issues.append(_issue("invalid_answer_cardinality", "单选或判断答案数量不符合题型，或多选答案重复，需人工核对。"))
    _, mapping_issues = _map_answers(answers, question["options"], question["type"])
    issues.extend(mapping_issues)
    option_texts = [_text(o["text"]) for o in question["options"]]
    option_labels = [_text(o["label"]).upper() for o in question["options"]]
    if len(set(option_texts)) != len(option_texts) or len(set(option_labels)) != len(option_labels):
        issues.append(_issue("duplicate_options", "选项标签或内容重复，需核对选项身份。"))
    for image in question["images"]:
        if image["required"] and not (image.get("path", "").strip() or image.get("url", "").strip()):
            issues.append(_issue("missing_required_image", "题目所需图片没有本地路径或来源链接。"))
        if image.get("field") == "option" and image["optionIndex"] >= len(question["options"]):
            issues.append(_issue("invalid_image_placement", "图片关联的选项下标超出选项范围；保留图片并核对位置。"))
    return _unique(issues)


def _refresh(question: dict) -> None:
    question["issues"] = _unique(
        [issue for issue in question["issues"] if issue["code"] not in _DERIVED_CODES]
        + _content_issues(question)
    )


def _source(source: dict, raw: dict, format_name: str) -> dict:
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    sha = source.get("sha256")
    if sha is None:
        sha = _digest(raw)
    if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
        raise ValueError("source.sha256 must be a 64-character SHA-256")
    result = {
        "id": "src-" + sha.lower()[:16],
        "path": _string(source.get("path"), "source.path", "<memory>"),
        "sha256": sha.lower(),
        "format": _string(source.get("format"), "source.format", format_name),
    }
    if isinstance(raw.get("source"), str):
        result["url"] = raw["source"]
    if isinstance(raw.get("ts"), (int, float)) and not isinstance(raw["ts"], bool):
        result["exportTimestamp"] = raw["ts"]
    for key in ("url", "exportTimestamp"):
        if key in source:
            result[key] = copy.deepcopy(source[key])
    return result


def _merge_sources(existing: list[dict], incoming: list[dict]) -> list[dict]:
    result = copy.deepcopy(existing)
    by_id = {source["id"]: source for source in result}
    for source in incoming:
        if source["id"] not in by_id:
            result.append(copy.deepcopy(source))
            by_id[source["id"]] = result[-1]
        else:
            old = by_id[source["id"]]
            if old["sha256"] != source["sha256"]:
                raise ValueError("source ID collision with different file hashes")
            if old["path"] != source["path"]:
                old["paths"] = _unique(old.get("paths", [old["path"]]) + [source["path"]])
    return result


def _variants(question: dict) -> list[dict]:
    if question.get("variants"):
        return copy.deepcopy(question["variants"])
    if question["correctAnswer"]:
        return [{"correctAnswer": copy.deepcopy(question["correctAnswer"]),
                 "sources": copy.deepcopy(question.get("correctAnswerSources", question["sources"]))}]
    return []


def _has_paragraph_block(text: str, block: str) -> bool:
    """Match a whole explanation block without altering its original content."""
    blank_line = r"\r?\n[ \t]*\r?\n"
    return bool(re.search(
        rf"(?:\A|{blank_line}){re.escape(block)}(?:\Z|{blank_line})", text))


def _merge_question(old: dict, new: dict) -> None:
    observed_variants = _variants(old) + _variants(new)
    old["sources"] = _unique(old["sources"] + new["sources"])
    variants = []
    by_answer = {}
    for variant in observed_variants:
        key = _answer_key(variant["correctAnswer"], old["type"])
        if key not in by_answer:
            variants.append(copy.deepcopy(variant))
            by_answer[key] = variants[-1]
        else:
            by_answer[key]["sources"] = _unique(by_answer[key]["sources"] + variant["sources"])
    if len(variants) > 1:
        old["variants"] = variants
        old["correctAnswer"] = []
        old["correctAnswerSources"] = []
    elif variants:
        old["correctAnswer"] = variants[0]["correctAnswer"]
        old["correctAnswerSources"] = variants[0]["sources"]
        old.pop("variants", None)
    old["rawCorrectAnswer"] = _unique(old.get("rawCorrectAnswer", []) + new.get("rawCorrectAnswer", []))
    for key in ("originalExplanation", "explanation"):
        if new[key] and not _has_paragraph_block(old[key], new[key]):
            old[key] = "\n\n".join(text for text in (old[key], new[key]) if text)
    # A concise, separately authored explanation is revised rather than joined
    # on every import. Empty or legacy input cannot erase an existing revision.
    if "briefExplanation" in new and (new["briefExplanation"] or "briefExplanation" not in old):
        old["briefExplanation"] = new["briefExplanation"]
    if "case" in new and new["case"] and not _has_paragraph_block(old.get("case", ""), new["case"]):
        old["case"] = "\n\n".join(text for text in (old.get("case", ""), new["case"]) if text)
    old["images"] = _unique(old["images"] + new["images"])
    old["topicIds"] = _unique(old["topicIds"] + new["topicIds"])
    old["issues"] = _unique(old["issues"] + new["issues"])
    # A later source with a valid answer can repair a missing-answer observation.
    if new["correctAnswer"] and old["correctAnswer"] and not any(i["code"] in {"unmapped_answer", "ambiguous_answer"} for i in new["issues"]):
        old["issues"] = [i for i in old["issues"] if i["code"] not in {"unmapped_answer", "ambiguous_answer"}]
    _refresh(old)


def _normalize_topics(raw_topics: Any, course: str, remap: dict[str, str]) -> list[dict]:
    if raw_topics is None:
        return []
    if not isinstance(raw_topics, list):
        raise ValueError("topics must be an array")
    topics = []
    for raw in raw_topics:
        if not isinstance(raw, dict):
            raise ValueError("topics entries must be objects")
        title = _string(raw.get("title"), "topic.title")
        topic = {
            "id": _string(raw.get("id"), "topic.id") or "t-" + _digest([_text(course), _text(title)])[:24],
            "title": title, "summary": _string(raw.get("summary"), "topic.summary"),
            "explanation": _string(raw.get("explanation"), "topic.explanation"),
            "questionIds": _remap_ids(raw.get("questionIds", []), remap, "topic.questionIds"),
            "cards": [],
        }
        cards = raw.get("cards", [])
        if not isinstance(cards, list):
            raise ValueError("topic.cards must be an array")
        for card in cards:
            if not isinstance(card, dict):
                raise ValueError("cards entries must be objects")
            prompt = _string(card.get("prompt"), "card.prompt")
            topic["cards"].append({
                "id": _string(card.get("id"), "card.id") or "c-" + _digest([topic["id"], prompt])[:24],
                "prompt": prompt, "answer": _string(card.get("answer"), "card.answer"),
                "questionIds": _remap_ids(card.get("questionIds", []), remap, "card.questionIds"),
            })
        topics.append(topic)
    return topics


def _remap_ids(ids: Any, remap: dict[str, str], name: str) -> list[str]:
    if not isinstance(ids, list) or not all(isinstance(value, (str, int)) and not isinstance(value, bool) for value in ids):
        raise ValueError(f"{name} must be an array of identifiers")
    return _unique([remap.get(str(value), str(value)) for value in ids])


def _remove_conflict_cards(bank: dict) -> None:
    conflicts = {q["id"] for q in bank["questions"] if len(q.get("variants", [])) > 1}
    for topic in bank["topics"]:
        topic["cards"] = [card for card in topic["cards"] if not conflicts.intersection(card["questionIds"])]


def _locator(item: dict, key: str, index: int) -> str:
    """Prefer document page/line positions; keep scrape indices as a fallback."""
    parts = []
    pages = item.get("sourcePages")
    if pages is not None and (not isinstance(pages, list) or not all(
        isinstance(page, (int, str)) and not isinstance(page, bool) for page in pages
    )):
        raise ValueError("sourcePages must be an array of page numbers")
    pages = [str(page) for page in (pages or []) if str(page).strip()]
    if not pages and item.get("sourcePage") is not None:
        page = item["sourcePage"]
        if not isinstance(page, (int, str)) or isinstance(page, bool):
            raise ValueError("sourcePage must be a page number")
        if str(page).strip():
            pages = [str(page)]
    if pages:
        parts.append("page=" + ",".join(_unique(pages)))
    if item.get("sourceLine") is not None:
        line = item["sourceLine"]
        if not isinstance(line, (int, str)) or isinstance(line, bool):
            raise ValueError("sourceLine must be a line number")
        if str(line).strip():
            parts.append("line=" + str(line))
    serial = item.get("sourceQuestionNumber", item.get("id"))
    if serial is not None and not isinstance(serial, (int, str)):
        raise ValueError("source question number must be text or an integer")
    if not parts:
        parts.append(f"{key}[{index}]")
    if serial is not None and str(serial).strip():
        parts.append("id=" + str(serial))
    return ";".join(parts)


def normalize(raw: dict, course: str, source: dict) -> dict:
    """Normalize Good Review or canonical questions; reject malformed structure."""
    if not isinstance(raw, dict):
        raise ValueError("input must be a JSON object")
    if not isinstance(course, str) or not course.strip():
        raise ValueError("course must be a non-empty string")
    course = course.strip()
    key = "items" if "items" in raw else "questions"
    items = raw.get(key)
    if not isinstance(items, list):
        raise ValueError("input must contain an items or questions array")
    source_entry = _source(source, raw, "goodreview" if key == "items" else "normalized")
    original_sources = copy.deepcopy(raw.get("sources", [])) if key == "questions" else []
    if not isinstance(original_sources, list):
        raise ValueError("sources must be an array")
    for entry in original_sources:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("id", "path", "sha256", "format")):
            raise ValueError("source entries require id, path, sha256, format strings")
    bank = {"schemaVersion": 1, "course": course,
            "sources": _merge_sources(original_sources, [source_entry]), "questions": [], "topics": []}
    by_id = {}
    remap = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("question entries must be objects")
        item = strip_personal_answers(item)
        raw_type = _string(item.get("type"), "question.type", "未知")
        kind = TYPE_ALIASES.get(raw_type.strip().lower(), raw_type.strip())
        if kind not in TYPES:
            kind = "未知"
        options = _options(item.get("options"))
        original_correct = _answers(item.get("correctAnswer"), "correctAnswer")
        correct, answer_issues = _map_answers(original_correct, options, kind)
        issues = item.get("issues", [])
        if not isinstance(issues, list) or not all(isinstance(i, dict) and isinstance(i.get("code"), str) and isinstance(i.get("message"), str) for i in issues):
            raise ValueError("issues must contain code and message strings")
        question = {
            "id": "", "type": kind, "title": _string(item.get("title"), "question.title"),
            "options": options, "correctAnswer": correct,
            "rawCorrectAnswer": _answers(item.get("rawCorrectAnswer", original_correct), "rawCorrectAnswer"),
            "originalExplanation": _string(item.get("originalExplanation", item.get("analysis")), "originalExplanation"),
            "explanation": _string(item.get("explanation"), "explanation"),
            "topicIds": _remap_ids(item.get("topicIds", []), {}, "topicIds"),
            "images": _images(item.get("images")),
            "sources": _unique(_refs(item.get("sources"), "question.sources") + [{
                "sourceId": source_entry["id"], "locator": _locator(item, key, index)}]),
            "issues": _unique(copy.deepcopy(issues) + answer_issues),
        }
        if "briefExplanation" in item:
            if not isinstance(item["briefExplanation"], str):
                raise ValueError("briefExplanation must be a string")
            question["briefExplanation"] = item["briefExplanation"]
        if "case" in item:
            question["case"] = _string(item["case"], "question.case")
        if "rawType" in item:
            question["rawType"] = _string(item["rawType"], "rawType")
        elif raw_type != kind:
            question["rawType"] = raw_type
        if "rawText" in item:
            question["rawText"] = _string(item["rawText"], "rawText")
        for location_key in ("sourcePages", "sourcePage", "sourceLine", "sourceQuestionNumber"):
            if location_key in item:
                question[location_key] = copy.deepcopy(item[location_key])
        question["correctAnswerSources"] = (
            _refs(item.get("correctAnswerSources"), "correctAnswerSources")
            if "correctAnswerSources" in item
            else copy.deepcopy(question["sources"]) if correct else []
        )
        if item.get("variants"):
            if not isinstance(item["variants"], list):
                raise ValueError("variants must be an array")
            question["variants"] = []
            for variant in item["variants"]:
                if not isinstance(variant, dict):
                    raise ValueError("variants entries must be objects")
                question["variants"].append({
                    "correctAnswer": _answers(variant.get("correctAnswer"), "variant.correctAnswer"),
                    "sources": _refs(variant.get("sources"), "variant.sources"),
                })
            if len(question["variants"]) > 1:
                question["correctAnswer"] = []
                question["correctAnswerSources"] = []
        question["id"] = _question_id(course, question)
        if "id" in item:
            remap[str(item["id"])] = question["id"]
        _refresh(question)
        if question["id"] in by_id:
            _merge_question(by_id[question["id"]], question)
        else:
            bank["questions"].append(question)
            by_id[question["id"]] = question
    bank["topics"] = _normalize_topics(strip_personal_answers(raw.get("topics")), course, remap)
    _remove_conflict_cards(bank)
    bank = strip_personal_answers(bank)
    validate_bank(bank)
    return bank


def merge_banks(existing: dict, incoming: dict) -> dict:
    """Merge stable question identities, retaining all observed answer versions."""
    validate_bank(existing)
    validate_bank(incoming)
    if _text(existing["course"]) != _text(incoming["course"]):
        raise ValueError("cannot merge banks from different courses")
    bank = strip_personal_answers(existing)
    incoming = strip_personal_answers(incoming)
    bank["sources"] = _merge_sources(bank["sources"], incoming["sources"])
    by_id = {question["id"]: question for question in bank["questions"]}
    for question in incoming["questions"]:
        if question["id"] in by_id:
            _merge_question(by_id[question["id"]], question)
        else:
            bank["questions"].append(copy.deepcopy(question))
            by_id[question["id"]] = bank["questions"][-1]
    by_topic = {topic["id"]: topic for topic in bank["topics"]}
    for topic in incoming["topics"]:
        if topic["id"] not in by_topic:
            bank["topics"].append(copy.deepcopy(topic))
            by_topic[topic["id"]] = bank["topics"][-1]
        else:
            old = by_topic[topic["id"]]
            old["questionIds"] = _unique(old["questionIds"] + topic["questionIds"])
            old["cards"] = _unique(old["cards"] + topic["cards"])
            for key in ("summary", "explanation"):
                old[key] = "\n\n".join(_unique([value for value in (old[key], topic[key]) if value]))
    _remove_conflict_cards(bank)
    validate_bank(bank)
    return bank


def _sample_features(question: dict) -> set[str]:
    features = {"type:" + question["type"]}
    if re.search(r"[=$∫Σ√^]|\\(?:frac|sum|int|begin|\(|\[)|[α-ωΑ-Ω]", question.get("case", "") + question["title"] + " ".join(o["text"] for o in question["options"])):
        features.add("formula")
    if question["images"]:
        features.add("image")
    answers = question["correctAnswer"] + [answer for variant in question.get("variants", []) for answer in variant["correctAnswer"]]
    if any(len(answer) >= 100 for answer in answers):
        features.add("long_answer")
    if question["issues"] or question.get("variants"):
        features.add("issue")
    return features


def select_sample(bank: dict, limit: int = 10) -> dict:
    """Choose a deterministic coverage sample and retain its linked topics."""
    validate_bank(bank)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    result = strip_personal_answers(bank)
    questions = result["questions"]
    features = [_sample_features(question) for question in questions]
    uncovered = set().union(*features) if features else set()
    chosen = []
    remaining = set(range(len(questions)))
    while remaining and len(chosen) < limit:
        index = max(sorted(remaining), key=lambda i: (len(features[i] & uncovered), -i))
        chosen.append(index)
        remaining.remove(index)
        uncovered -= features[index]
    selected = {questions[index]["id"] for index in chosen}
    result["questions"] = [copy.deepcopy(questions[index]) for index in chosen]
    retained_topics = []
    for topic in result["topics"]:
        linked = selected.intersection(topic["questionIds"])
        linked.update(q["id"] for q in result["questions"] if topic["id"] in q["topicIds"])
        if not linked:
            continue
        topic["questionIds"] = [q["id"] for q in result["questions"] if q["id"] in linked]
        cards = []
        for card in topic["cards"]:
            refs = [qid for qid in card["questionIds"] if qid in selected]
            if refs:
                card["questionIds"] = refs
                cards.append(card)
        topic["cards"] = cards
        retained_topics.append(topic)
    result["topics"] = retained_topics
    available_topics = {topic["id"] for topic in retained_topics}
    for question in result["questions"]:
        question["topicIds"] = [tid for tid in question["topicIds"] if tid in available_topics]
    _remove_conflict_cards(result)
    return result


def validate_bank(bank: dict) -> list[dict]:
    """Raise for invalid shape and return contextual diagnostics for content."""
    if not isinstance(bank, dict) or type(bank.get("schemaVersion")) is not int or bank["schemaVersion"] != 1:
        raise ValueError("bank.schemaVersion must be 1")
    if not isinstance(bank.get("course"), str) or not bank["course"].strip():
        raise ValueError("bank.course must be a non-empty string")
    for key in ("sources", "questions", "topics"):
        if not isinstance(bank.get(key), list):
            raise ValueError(f"bank.{key} must be an array")
    try:
        json.dumps(bank, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("bank must contain only JSON-serializable finite values") from error
    source_ids = set()
    for source in bank["sources"]:
        if not isinstance(source, dict) or not all(isinstance(source.get(key), str) for key in ("id", "path", "sha256", "format")):
            raise ValueError("sources require id, path, sha256, format strings")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", source["sha256"]):
            raise ValueError("source.sha256 must be a SHA-256")
        if not source["id"]:
            raise ValueError("source.id must be non-empty")
        if source["id"] in source_ids:
            raise ValueError("duplicate source ID")
        source_ids.add(source["id"])
    question_ids = set()
    diagnostics = []
    for question in bank["questions"]:
        if not isinstance(question, dict):
            raise ValueError("questions entries must be objects")
        for key in ("id", "type", "title", "originalExplanation", "explanation"):
            if not isinstance(question.get(key), str):
                raise ValueError(f"question.{key} must be a string")
        if "briefExplanation" in question and not isinstance(question["briefExplanation"], str):
            raise ValueError("question.briefExplanation must be a string")
        if "case" in question and not isinstance(question["case"], str):
            raise ValueError("question.case must be a string")
        if question["type"] not in TYPES:
            raise ValueError("question.type must be a canonical Chinese type")
        if not question["id"] or question["id"] in question_ids:
            raise ValueError("question IDs must be non-empty and unique")
        question_ids.add(question["id"])
        for key in ("correctAnswer", "topicIds"):
            if not isinstance(question.get(key), list) or not all(isinstance(value, str) for value in question[key]):
                raise ValueError(f"question.{key} must be a string array")
        if not isinstance(question.get("options"), list) or not all(isinstance(option, dict) and all(isinstance(option.get(k), str) for k in ("label", "text")) for option in question["options"]):
            raise ValueError("question.options must contain label and text strings")
        if not isinstance(question.get("images"), list) or not all(isinstance(image, dict) and isinstance(image.get("required"), bool) and all(isinstance(image[k], str) for k in ("path", "url") if k in image) for image in question["images"]):
            raise ValueError("question.images must contain required boolean and optional path/url strings")
        _images(question["images"])
        refs = _refs(question.get("sources"), "question.sources")
        if "sources" not in question:
            raise ValueError("question.sources is required")
        if not isinstance(question.get("issues"), list) or not all(isinstance(issue, dict) and all(isinstance(issue.get(k), str) for k in ("code", "message")) for issue in question["issues"]):
            raise ValueError("question.issues must contain code and message strings")
        variants = question.get("variants", [])
        if not isinstance(variants, list):
            raise ValueError("question.variants must be an array")
        for variant in variants:
            if not isinstance(variant, dict) or not isinstance(variant.get("correctAnswer"), list) or not all(isinstance(a, str) for a in variant["correctAnswer"]):
                raise ValueError("variant.correctAnswer must be a string array")
            _refs(variant.get("sources"), "variant.sources")
        issues = _unique(question["issues"] + _content_issues(question))
        if not refs or any(ref["sourceId"] not in source_ids for ref in refs):
            issues.append(_issue("missing_source_reference", "题目引用的原始来源未登记。"))
        diagnostics.extend(dict(issue, questionId=question["id"]) for issue in _unique(issues))
    topic_ids = set()
    conflicts = {q["id"] for q in bank["questions"] if len(q.get("variants", [])) > 1}
    for topic in bank["topics"]:
        if not isinstance(topic, dict) or not all(isinstance(topic.get(key), str) for key in ("id", "title", "summary", "explanation")):
            raise ValueError("topics require id, title, summary, explanation strings")
        if not topic["id"] or topic["id"] in topic_ids:
            raise ValueError("topic IDs must be non-empty and unique")
        topic_ids.add(topic["id"])
        if not isinstance(topic.get("questionIds"), list) or not all(isinstance(qid, str) for qid in topic["questionIds"]):
            raise ValueError("topic.questionIds must be a string array")
        if any(qid not in question_ids for qid in topic["questionIds"]):
            diagnostics.append(dict(_issue("unknown_question_reference", "知识点引用了未登记题目。"), topicId=topic["id"]))
        if not isinstance(topic.get("cards"), list):
            raise ValueError("topic.cards must be an array")
        card_ids = set()
        for card in topic["cards"]:
            if not isinstance(card, dict) or not all(isinstance(card.get(key), str) for key in ("id", "prompt", "answer")) or not isinstance(card.get("questionIds"), list) or not all(isinstance(qid, str) for qid in card["questionIds"]):
                raise ValueError("cards require id, prompt, answer strings and questionIds array")
            if not card["id"] or card["id"] in card_ids:
                raise ValueError("card IDs must be non-empty and unique within a topic")
            card_ids.add(card["id"])
            if not card["questionIds"] or any(qid not in question_ids for qid in card["questionIds"]):
                diagnostics.append(dict(_issue("unknown_question_reference", "复习卡引用了未登记题目。"), cardId=card["id"]))
            if conflicts.intersection(card["questionIds"]):
                diagnostics.append(dict(_issue("unsafe_conflict_card", "答案存在冲突的题目不能生成确定答案卡。"), cardId=card["id"]))
    for question in bank["questions"]:
        if any(tid not in topic_ids for tid in question["topicIds"]):
            diagnostics.append(dict(_issue("unknown_topic_reference", "题目引用了未登记知识点。"), questionId=question["id"]))
    return _unique(diagnostics)
