"""Contract tests for preservation, provenance, conflicts and sample coverage."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from questionbank.core import (
    UNSAFE_CARD_CODES, file_sha256, merge_banks, normalize, select_sample,
    strip_personal_answers, validate_bank,
)


COURSE = "电路原理"


def source(number=1):
    return {"path": f"raw/source-{number}.json", "sha256": str(number) * 64,
            "format": "goodreview"}


def question(kind="单选", title="欧姆定律的表达式是？", correct=None, **extra):
    data = {
        "id": 1, "type": kind, "title": title,
        "options": [{"label": "A", "text": "U=IR"}, {"label": "B", "text": "U=I/R"}],
        "correctAnswer": ["A"] if correct is None else correct,
        "selectedAnswer": ["B"], "images": [],
    }
    data.update(extra)
    return data


def bank(items, number=1, **extra):
    return normalize(dict(items=items, **extra), COURSE, source(number))


def codes(q):
    return {issue["code"] for issue in q["issues"]}


class NormalizeTests(unittest.TestCase):
    def test_explicit_personal_body_fields_are_removed_before_answer_mapping(self):
        raw = question(
            title="实际题干。\n用户答案：PRIVATE_TITLE\nPRIVATE_TITLE_MORE",
            correct=["U=IR\n我的答案：PRIVATE_CORRECT\n2. PRIVATE_CORRECT_STEP"],
            options=[{"label": "A", "text": "U=IR\n个人作答：PRIVATE_OPTION"},
                     {"label": "B", "text": "U=I/R"}],
            originalExplanation="可靠原解析\n我的答案：PRIVATE_ORIGINAL\nPRIVATE_MORE",
            explanation="有依据补充\n学生答案：PRIVATE_EXPLANATION",
            briefExplanation="简要解析\n你的答案：PRIVATE_BRIEF",
            case="已有案例\n用户作答：PRIVATE_CASE",
        )
        before = copy.deepcopy(raw)
        result = bank([raw])
        q = result["questions"][0]
        self.assertEqual(q["title"].strip(), "实际题干。")
        self.assertEqual(q["correctAnswer"][0].strip(), "U=IR")
        self.assertEqual(q["options"][0]["text"].strip(), "U=IR")
        self.assertEqual(q["originalExplanation"].strip(), "可靠原解析")
        self.assertEqual(q["case"].strip(), "已有案例")
        self.assertNotIn("PRIVATE", json.dumps(result, ensure_ascii=False))
        self.assertEqual(validate_bank(result), [])
        self.assertEqual(raw, before)

    def test_personal_only_correct_answer_cannot_become_a_confirmed_answer(self):
        normalized = bank([question("问答", correct=["我的答案：PRIVATE\n2. PRIVATE_STEP"], options=[])])
        q = normalized["questions"][0]
        self.assertEqual(q["correctAnswer"], [])
        self.assertIn("missing_correct_answer", codes(q))
        self.assertNotIn("PRIVATE", json.dumps(normalized, ensure_ascii=False))

    def test_indeterminate_and_qa_aliases_preserve_their_semantics(self):
        for alias in ("不定项", "不定项题", "不定项选择", "不定项选择题", "任选", "任选题", "任选选择题"):
            for answer in (["a"], ["a/b"], ["B", "A"]):
                with self.subTest(alias=alias, answer=answer):
                    normalized = bank([question(alias, correct=answer)])
                    q = normalized["questions"][0]
                    self.assertEqual(q["type"], "不定项")
                    expected = ["U=IR"] if len(answer) == 1 and answer[0] == "a" else (
                        ["U=I/R", "U=IR"] if len(answer) == 2 else ["U=IR", "U=I/R"])
                    self.assertEqual(q["correctAnswer"], expected)
                    self.assertEqual(validate_bank(normalized), [])
        for alias in ("问答", "问答题"):
            with self.subTest(alias=alias):
                self.assertEqual(bank([question(alias, correct=["完整答案"], options=[])])["questions"][0]["type"], "简答")

    def test_indeterminate_duplicate_answers_still_require_review(self):
        q = bank([question("任选", correct=["A", "A"])])["questions"][0]
        self.assertIn("invalid_answer_cardinality", codes(q))

    def test_image_placement_and_case_survive_normalization_without_new_ids(self):
        baseline = bank([question()])["questions"][0]
        media = [{"path": f"media/{field}.png", "required": True, "field": field,
                  **({"optionIndex": 1} if field == "option" else {}), "alt": "原图说明"}
                 for field in ("title", "option", "answer", "explanation")]
        raw = question(images=media, case="现有案例：给定电压与电阻。")
        before = copy.deepcopy(raw)
        normalized = bank([raw])
        q = normalized["questions"][0]
        self.assertEqual(q["images"], media)
        self.assertEqual(q["case"], raw["case"])
        self.assertEqual(q["id"], baseline["id"])
        self.assertEqual(raw, before)
        self.assertEqual(validate_bank(normalized), [])

    def test_malformed_image_placement_is_rejected_but_legacy_media_is_allowed(self):
        malformed = [
            {"field": "case"}, {"field": []}, {"field": "option"},
            {"field": "option", "optionIndex": True}, {"field": "option", "optionIndex": -1},
            {"field": "option", "optionIndex": "1"}, {"field": "title", "optionIndex": 0},
            {"optionIndex": 0},
        ]
        for placement in malformed:
            with self.subTest(placement=placement), self.assertRaises(ValueError):
                bank([question(images=[{"path": "graph.png", "required": True, **placement}])])
            canonical = bank([question()])
            canonical["questions"][0]["images"] = [{"path": "graph.png", "required": True, **placement}]
            before = copy.deepcopy(canonical)
            with self.subTest(canonical_placement=placement), self.assertRaises(ValueError):
                validate_bank(canonical)
            self.assertEqual(canonical, before)
        legacy = bank([question(images=[{"path": "graph.png", "required": True}])])
        self.assertEqual(validate_bank(legacy), [])
        self.assertNotIn("field", legacy["questions"][0]["images"][0])
        out_of_range = bank([question(images=[{"path": "graph.png", "required": True,
                                              "field": "option", "optionIndex": 2}])])
        self.assertIn("invalid_image_placement", codes(out_of_range["questions"][0]))
        self.assertEqual(out_of_range["questions"][0]["images"][0]["path"], "graph.png")

    def test_official_judgment_y_and_n_are_unambiguous(self):
        for original, expected in (("Y", "正确"), ("N", "错误")):
            with self.subTest(original=original):
                normalized = bank([question("判断", correct=[original], options=[])])
                self.assertEqual(normalized["questions"][0]["correctAnswer"], [expected])
                self.assertEqual(validate_bank(normalized), [])

    def test_brief_explanation_stays_separate_and_does_not_truncate_full_text(self):
        original = "原解析需要完整保留，包含题目条件与各选项说明。" * 80
        supplementary = "补充解释及查证依据。" * 40
        brief = "由 U=IR 可直接判定正确选项。"
        item = question(originalExplanation=original, explanation=supplementary,
                        briefExplanation=brief)
        before = copy.deepcopy(item)
        for key in ("items", "questions"):
            with self.subTest(input_format=key):
                normalized = normalize({key: [item]}, COURSE, source())
                result = normalized["questions"][0]
                self.assertEqual(result["originalExplanation"], original)
                self.assertEqual(result["explanation"], supplementary)
                self.assertEqual(result["briefExplanation"], brief)
                self.assertEqual(validate_bank(normalized), [])
        self.assertEqual(item, before)

    def test_optional_brief_explanation_does_not_change_identity_answers_or_issues(self):
        legacy = bank([question()])
        self.assertNotIn("briefExplanation", legacy["questions"][0])
        self.assertEqual(validate_bank(legacy), [])
        amended = bank([question(briefExplanation="依照欧姆定律判断。")])
        first, revised = legacy["questions"][0], amended["questions"][0]
        for key in ("id", "correctAnswer", "rawCorrectAnswer", "issues"):
            self.assertEqual(first[key], revised[key])
        self.assertEqual(select_sample(amended)["questions"][0]["briefExplanation"],
                         "依照欧姆定律判断。")

    def test_non_string_brief_explanations_are_rejected_on_input(self):
        for value in (None, 123, False, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bank([question(briefExplanation=value)])

    def test_five_types_map_answers_and_preserve_source_and_explanations(self):
        items = [
            question("single"),
            question("multiple", "哪些式子是恒等式？", ["AB"]),
            question("true_false", "导体电阻总为正。", ["true"],
                     options=[{"label": "true", "text": "正确"}, {"label": "false", "text": "错误"}]),
            question("fill", "电流单位是____。", ["安培"], options=[]),
            question("short", "解释电压。", ["单位电荷在两点之间电势能的差值。"], options=[],
                     originalExplanation="来自讲义的原始解析", explanation="补充的理解方法"),
        ]
        raw = {"source": "https://example.invalid/work/view?private=value", "ts": 123, "items": items}
        before = copy.deepcopy(raw)
        result = normalize(raw, COURSE, source())
        self.assertEqual([q["type"] for q in result["questions"]], ["单选", "多选", "判断", "填空", "简答"])
        self.assertEqual(result["questions"][0]["correctAnswer"], ["U=IR"])
        for q in result["questions"]:
            self.assertNotIn("selectedAnswer", q)
            self.assertNotIn("rawSelectedAnswer", q)
        self.assertEqual(result["questions"][1]["correctAnswer"], ["U=IR", "U=I/R"])
        self.assertEqual(result["questions"][2]["correctAnswer"], ["正确"])
        self.assertEqual(result["questions"][4]["originalExplanation"], "来自讲义的原始解析")
        self.assertEqual(result["questions"][4]["explanation"], "补充的理解方法")
        self.assertEqual(result["sources"][0]["id"], "src-" + "1" * 16)
        self.assertEqual(result["sources"][0]["url"], raw["source"])
        self.assertEqual(result["sources"][0]["exportTimestamp"], 123)
        self.assertEqual(raw, before)
        self.assertEqual(validate_bank(result), [])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_missing_correct_answer_does_not_use_learner_answer(self):
        result = bank([question(correct=[])])["questions"][0]
        self.assertEqual(result["correctAnswer"], [])
        self.assertNotIn("selectedAnswer", result)
        self.assertNotIn("rawSelectedAnswer", result)
        self.assertIn("missing_correct_answer", codes(result))

    def test_personal_answer_fields_are_ignored_even_if_malformed(self):
        raw = {"items": [question(selectedAnswer={"discard": "personal"},
                                  rawSelectedAnswer="discarded raw answer")]}
        before = copy.deepcopy(raw)
        result = normalize(raw, COURSE, source())
        self.assertEqual(result["questions"][0]["correctAnswer"], ["U=IR"])
        self.assertNotIn("selectedAnswer", result["questions"][0])
        self.assertNotIn("rawSelectedAnswer", result["questions"][0])
        self.assertEqual(raw, before)

    def test_canonical_variant_personal_answers_are_not_normalized(self):
        raw = {"questions": [question(correct=[], variants=[
            {"correctAnswer": ["U=IR"], "sources": [],
             "selectedAnswer": ["learner choice"], "rawSelectedAnswer": ["B"]},
            {"correctAnswer": ["U=I/R"], "sources": [],
             "selectedAnswer": ["learner choice 2"], "rawSelectedAnswer": ["A"]},
        ])]}
        before = copy.deepcopy(raw)
        result = normalize(raw, COURSE, source())
        q = result["questions"][0]
        self.assertEqual([v["correctAnswer"] for v in q["variants"]], [["U=IR"], ["U=I/R"]])
        self.assertEqual(strip_personal_answers(result), result)
        self.assertEqual(raw, before)

    def test_decimals_and_english_long_answers_are_not_truncated(self):
        english = "The value is one point five. " * 20
        items = [question("填空", "输入数值", ["1.5", "(1) 3.14"], options=[]),
                 question("简答", "Explain the phenomenon", [english], options=[])]
        result = bank(items)
        self.assertEqual(result["questions"][0]["correctAnswer"], ["1.5", "(1) 3.14"])
        self.assertEqual(result["questions"][1]["correctAnswer"], [english])
        self.assertEqual(result["questions"][1]["rawCorrectAnswer"], [english])

    def test_identity_ignores_number_time_answer_and_source_but_keeps_options_and_course(self):
        first = bank([question(title="1. 欧姆定律？", correct=["A"])])
        second = bank([question(title="99、欧姆定律？", correct=["B"])], 2, ts=999)
        self.assertEqual(first["questions"][0]["id"], second["questions"][0]["id"])
        altered = question(title="欧姆定律？", options=[{"label": "A", "text": "U=IR"}, {"label": "B", "text": "U=R/I"}])
        third = bank([altered])
        self.assertNotEqual(first["questions"][0]["id"], third["questions"][0]["id"])
        other_course = normalize({"items": [question(title="欧姆定律？")]}, "其他课程", source())
        self.assertNotEqual(first["questions"][0]["id"], other_course["questions"][0]["id"])
        reordered = copy.deepcopy(altered)
        reordered["options"].reverse()
        self.assertNotEqual(third["questions"][0]["id"], bank([reordered])["questions"][0]["id"])

    def test_decimal_title_is_not_interpreted_as_numbering(self):
        first = bank([question("填空", "1.5伏时的电流", ["1"], options=[])])
        second = bank([question("填空", "5伏时的电流", ["1"], options=[])])
        self.assertNotEqual(first["questions"][0]["id"], second["questions"][0]["id"])
        fullwidth = bank([question("填空", "１．５伏时的电流", ["1"], options=[])])
        self.assertEqual(first["questions"][0]["id"], fullwidth["questions"][0]["id"])

    def test_multiple_letter_delimiters_and_raw_values(self):
        result = bank([question("多选", correct=["A、B"])])["questions"][0]
        self.assertEqual(result["correctAnswer"], ["U=IR", "U=I/R"])
        self.assertEqual(result["rawCorrectAnswer"], ["A、B"])
        self.assertEqual(result["issues"], [])

    def test_unmappable_or_ambiguous_answers_are_reported(self):
        unknown = bank([question(correct=["Z"])])["questions"][0]
        self.assertEqual(unknown["correctAnswer"], ["Z"])
        self.assertIn("unmapped_answer", codes(unknown))
        ambiguous = bank([question(correct=["A"], options=[{"label": "A", "text": "一"}, {"label": "A", "text": "二"}])])["questions"][0]
        self.assertIn("ambiguous_answer", codes(ambiguous))
        self.assertIn("duplicate_options", codes(ambiguous))

    def test_unknown_type_and_required_image_loss_are_visible(self):
        result = bank([question("计算题", images=[{"required": True}])])["questions"][0]
        self.assertEqual(result["type"], "未知")
        self.assertEqual(result["rawType"], "计算题")
        self.assertIn("unknown_type", codes(result))
        self.assertIn("missing_required_image", codes(result))
        available = bank([question(images=["https://example.invalid/image.png"])])["questions"][0]
        self.assertEqual(available["images"], [{"url": "https://example.invalid/image.png", "required": True}])
        self.assertNotIn("missing_required_image", codes(available))

    def test_normalized_questions_preserve_topics_and_remap_original_ids(self):
        raw = {"questions": [question("fill", "测量单位？", ["伏特"], options=[], topicIds=["voltage"])],
               "topics": [{"id": "voltage", "title": "电压", "summary": "电势差", "explanation": "",
                           "questionIds": [1], "cards": [{"id": "card1", "prompt": "电压单位？", "answer": "伏特", "questionIds": [1]}]}]}
        result = normalize(raw, COURSE, source())
        qid = result["questions"][0]["id"]
        self.assertEqual(result["topics"][0]["questionIds"], [qid])
        self.assertEqual(result["topics"][0]["cards"][0]["questionIds"], [qid])
        self.assertEqual(result["questions"][0]["topicIds"], ["voltage"])
        self.assertEqual(validate_bank(result), [])

    def test_document_locators_prefer_pages_lines_and_source_number(self):
        text = "7、计算题\n原始换行 1.5 必须保留"
        result = bank([question("未知", rawType="计算题", rawText=text,
                                id="", sourcePages=[2, 3], sourcePage=2,
                                sourceLine=18, sourceQuestionNumber="7")])
        q = result["questions"][0]
        self.assertEqual(q["sources"][0]["locator"], "page=2,3;line=18;id=7")
        self.assertEqual(q["rawType"], "计算题")
        self.assertEqual(q["rawText"], text)
        self.assertEqual(q["sourcePages"], [2, 3])
        page_only = bank([question(sourcePage=4)])["questions"][0]
        self.assertEqual(page_only["sources"][0]["locator"], "page=4;id=1")
        line_only = bank([question(sourceLine=12)])["questions"][0]
        self.assertEqual(line_only["sources"][0]["locator"], "line=12;id=1")
        scrape = bank([question()])["questions"][0]
        self.assertEqual(scrape["sources"][0]["locator"], "items[0];id=1")

    def test_raw_type_is_kept_even_when_intake_already_canonicalized_it(self):
        q = bank([question("单选", rawType="单项选择题")])["questions"][0]
        self.assertEqual(q["type"], "单选")
        self.assertEqual(q["rawType"], "单项选择题")

    def test_malformed_document_locators_are_rejected(self):
        for extra in ({"sourcePages": "2"}, {"sourcePage": {}}, {"sourceLine": True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                bank([question(**extra)])

    def test_invalid_structures_fail_with_value_error(self):
        cases = [[], {}, {"items": {}}, {"items": [None]},
                 {"items": [question(correct="A")]},
                 {"items": [question(options=["A"])]},
                 {"items": [question(images=[{"required": "yes"}])]}]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize(raw, COURSE, source())
        with self.assertRaises(ValueError):
            normalize({"items": []}, "", source())
        with self.assertRaises(ValueError):
            normalize({"items": []}, COURSE, {"sha256": "invalid"})


class MergeTests(unittest.TestCase):
    def test_indeterminate_answer_order_is_not_a_false_conflict(self):
        merged = merge_banks(bank([question("任选", correct=["AB"])]),
                             bank([question("不定项选择题", correct=["B", "A"])], 2))
        self.assertEqual(len(merged["questions"]), 1)
        self.assertEqual(merged["questions"][0]["type"], "不定项")
        self.assertNotIn("variants", merged["questions"][0])

    def test_case_and_media_placement_merge_retains_content_and_stable_id(self):
        first = bank([question(case="已有案例。", images=[{"path": "graph.png", "required": True,
                                                       "field": "option", "optionIndex": 0}])])
        second = bank([question(case="补充案例条件。", images=[{"path": "graph.png", "required": True,
                                                            "field": "option", "optionIndex": 1}])], 2)
        merged = merge_banks(first, second)
        q = merged["questions"][0]
        self.assertEqual(q["case"], "已有案例。\n\n补充案例条件。")
        self.assertEqual([image["optionIndex"] for image in q["images"]], [0, 1])
        self.assertEqual(q["id"], first["questions"][0]["id"])
        self.assertEqual(merge_banks(merged, second), merged)

    def test_repeated_explanation_blocks_are_not_appended_again(self):
        original = "原解析第一段，保留行内换行。\n第二行。\r\n \r\n原解析第二段。"
        supplementary = "补充解释第一段。\n\n补充解释第二段。"
        new_original = "新增原解析第一段。\n\n新增原解析第二段。"
        new_supplementary = "新增补充第一段。\n\n新增补充第二段。"
        first = bank([question(originalExplanation=original, explanation=supplementary)])
        second = bank([question(originalExplanation=new_original,
                                explanation=new_supplementary)], 2)
        merged = merge_banks(first, second)
        result = merged["questions"][0]
        self.assertEqual(result["originalExplanation"], original + "\n\n" + new_original)
        self.assertEqual(result["explanation"], supplementary + "\n\n" + new_supplementary)
        self.assertEqual(merge_banks(merged, second), merged)
        self.assertEqual(merge_banks(merged, first), merged)
        contained = bank([question(originalExplanation="原解析第二段。",
                                   explanation="新增补充第一段。")], 3)
        last = merge_banks(merged, contained)["questions"][0]
        self.assertEqual(last["originalExplanation"], result["originalExplanation"])
        self.assertEqual(last["explanation"], result["explanation"])

    def test_explanation_word_substrings_are_distinct_blocks(self):
        first = bank([question(originalExplanation="原解析包含关键字和完整条件。",
                               explanation="方法说明包含关键字并继续展开。")])
        second = bank([question(originalExplanation="关键字", explanation="关键字")], 2)
        merged = merge_banks(first, second)
        self.assertEqual(merged["questions"][0]["originalExplanation"],
                         "原解析包含关键字和完整条件。\n\n关键字")
        self.assertEqual(merged["questions"][0]["explanation"],
                         "方法说明包含关键字并继续展开。\n\n关键字")
        self.assertEqual(merge_banks(merged, second), merged)

    def test_brief_explanation_revision_replaces_only_brief_and_retains_long_fields(self):
        old = bank([question(originalExplanation="原解析", explanation="原补充",
                             briefExplanation="第一版简析。")])
        incoming = bank([question(originalExplanation="新增原解析", explanation="新增补充",
                                  briefExplanation="修订后的简析。")], 2)
        old_before, incoming_before = copy.deepcopy(old), copy.deepcopy(incoming)
        merged = merge_banks(old, incoming)
        result = merged["questions"][0]
        self.assertEqual(result["briefExplanation"], "修订后的简析。")
        self.assertEqual(result["originalExplanation"], "原解析\n\n新增原解析")
        self.assertEqual(result["explanation"], "原补充\n\n新增补充")
        self.assertEqual(result["id"], old["questions"][0]["id"])
        self.assertEqual(result["correctAnswer"], old["questions"][0]["correctAnswer"])
        self.assertEqual(result["issues"], old["questions"][0]["issues"])
        self.assertEqual(merge_banks(merged, incoming)["questions"][0]["briefExplanation"],
                         "修订后的简析。")
        self.assertEqual(old, old_before)
        self.assertEqual(incoming, incoming_before)

    def test_brief_explanation_merge_accepts_legacy_banks_and_empty_revisions(self):
        legacy = bank([question()])
        enriched = bank([question(briefExplanation="保留的简析。")], 2)
        merged = merge_banks(legacy, enriched)
        self.assertEqual(merged["questions"][0]["briefExplanation"], "保留的简析。")
        for incoming in (legacy, bank([question(briefExplanation="")], 3)):
            with self.subTest(incoming=incoming):
                updated = merge_banks(merged, incoming)
                self.assertEqual(updated["questions"][0]["briefExplanation"], "保留的简析。")
                self.assertEqual(validate_bank(updated), [])
        duplicate = normalize({"items": [question(), question(briefExplanation="新增简析。", id=2)]},
                              COURSE, source())
        self.assertEqual(len(duplicate["questions"]), 1)
        self.assertEqual(duplicate["questions"][0]["briefExplanation"], "新增简析。")

    def test_merging_legacy_banks_cannot_restore_personal_answers(self):
        legacy = merge_banks(
            bank([question(), question(title="保留的第二题")]),
            bank([question(correct=["B"])], 2))
        incoming = bank([question(correct=["B"]), question(title="新增的第三题")], 3)
        for value in (legacy, incoming):
            for q in value["questions"]:
                q["selectedAnswer"] = ["个人答案秘密"]
                q["rawSelectedAnswer"] = ["个人原始答案秘密"]
                for variant in q.get("variants", []):
                    variant["selectedAnswer"] = ["版本中的个人答案"]
                    variant["rawSelectedAnswer"] = ["版本中的原始个人答案"]
        legacy_before, incoming_before = copy.deepcopy(legacy), copy.deepcopy(incoming)
        merged = merge_banks(legacy, incoming)
        repeated = merge_banks(merged, legacy)
        for value in (merged, repeated):
            self.assertEqual(strip_personal_answers(value), value)
            self.assertEqual(len(value["questions"]), 3)
            conflicted = value["questions"][0]
            self.assertEqual(conflicted["correctAnswer"], [])
            self.assertEqual([v["correctAnswer"] for v in conflicted["variants"]], [["U=IR"], ["U=I/R"]])
            self.assertEqual(value["questions"][1]["correctAnswer"], ["U=IR"])
            self.assertEqual(value["questions"][2]["correctAnswer"], ["U=IR"])
        self.assertEqual(legacy, legacy_before)
        self.assertEqual(incoming, incoming_before)

    def test_duplicate_import_is_idempotent_and_keeps_all_locators(self):
        first = bank([question(), question(id=2)])
        self.assertEqual(len(first["questions"]), 1)
        self.assertEqual(len(first["questions"][0]["sources"]), 2)
        self.assertEqual(merge_banks(first, first), first)
        other_file = bank([question(id=100)], 2)
        merged = merge_banks(first, other_file)
        self.assertEqual(len(merged["questions"]), 1)
        self.assertEqual(len(merged["sources"]), 2)
        self.assertEqual(len(merged["questions"][0]["sources"]), 3)

    def test_different_options_are_never_collapsed(self):
        a = bank([question()])
        b = bank([question(options=[{"label": "A", "text": "U=IR"}, {"label": "B", "text": "U=RI"}])], 2)
        self.assertEqual(len(merge_banks(a, b)["questions"]), 2)

    def test_conflicting_answers_have_exact_variant_sources_and_remove_answer_cards(self):
        first = bank([question()])
        qid = first["questions"][0]["id"]
        first["topics"] = [{"id": "topic1", "title": "欧姆定律", "summary": "", "explanation": "",
                            "questionIds": [qid], "cards": [{"id": "card1", "prompt": "公式？", "answer": "U=IR", "questionIds": [qid]}]}]
        before = copy.deepcopy(first)
        second = bank([question(correct=["B"])], 2)
        merged = merge_banks(first, second)
        q = merged["questions"][0]
        self.assertEqual(q["correctAnswer"], [])
        self.assertEqual([v["correctAnswer"] for v in q["variants"]], [["U=IR"], ["U=I/R"]])
        self.assertEqual([v["sources"][0]["sourceId"] for v in q["variants"]], ["src-" + "1" * 16, "src-" + "2" * 16])
        self.assertEqual([len(v["sources"]) for v in q["variants"]], [1, 1])
        self.assertIn("conflicting_correct_answers", codes(q))
        self.assertEqual(merged["topics"][0]["cards"], [])
        self.assertEqual(merge_banks(merged, second), merged)
        self.assertEqual(first, before)

    def test_missing_answer_can_be_filled_by_a_later_correct_source(self):
        result = merge_banks(bank([question(correct=[])]), bank([question(correct=["A"])], 2))
        q = result["questions"][0]
        self.assertEqual(q["correctAnswer"], ["U=IR"])
        self.assertNotIn("missing_correct_answer", codes(q))
        self.assertNotIn("conflicting_correct_answers", codes(q))

    def test_answer_variants_do_not_credit_a_source_that_had_no_correct_answer(self):
        repaired = merge_banks(bank([question(correct=[])]), bank([question(correct=["A"])], 2))
        conflicted = merge_banks(repaired, bank([question(correct=["B"])], 3))
        variants = conflicted["questions"][0]["variants"]
        self.assertEqual([ref["sourceId"] for ref in variants[0]["sources"]], ["src-" + "2" * 16])
        self.assertEqual([ref["sourceId"] for ref in variants[1]["sources"]], ["src-" + "3" * 16])
        self.assertEqual(conflicted["questions"][0]["correctAnswerSources"], [])

    def test_multi_answer_order_is_not_a_false_conflict(self):
        result = merge_banks(bank([question("多选", correct=["AB"])]), bank([question("multiple", correct=["B", "A"])], 2))
        self.assertNotIn("conflicting_correct_answers", codes(result["questions"][0]))
        self.assertNotIn("variants", result["questions"][0])

    def test_merge_different_courses_is_rejected(self):
        other = normalize({"items": [question()]}, "不同课程", source(2))
        with self.assertRaises(ValueError):
            merge_banks(bank([question()]), other)


class SampleAndValidationTests(unittest.TestCase):
    def test_body_filter_keeps_unlabeled_steps_formula_code_and_source_metadata(self):
        unchanged = (
            "1. 保留题干条件\n2. 保留第二步\n$U=IR$\n"
            "未标注为个人答案的正确正文\n"
            "```text\n我的答案：literal code example\n2. code step\n```\n"
            "正文讨论我的答案这个字段，而非作答记录。\n"
        )
        value = {"title": unchanged, "case": unchanged, "correctAnswer": [unchanged],
                 "rawCorrectAnswer": [unchanged], "briefExplanation": unchanged,
                 "path": "我的答案：metadata-path", "url": "https://example.invalid/my-answer",
                 "sources": [{"locator": "用户答案：literal locator"}],
                 "topics": [{"explanation": unchanged, "cards": [{"answer": unchanged}]}]}
        before = copy.deepcopy(value)
        self.assertEqual(strip_personal_answers(value), before)
        self.assertEqual(value, before)
        marked = {"originalExplanation": "可靠解析\r\n你的答案：PRIVATE\r\n2. PRIVATE_STEP\r\n解析：保留后续解析\r\n$P=UI$\r\n"}
        cleaned = strip_personal_answers(marked)
        self.assertNotIn("PRIVATE", cleaned["originalExplanation"])
        self.assertIn("解析：保留后续解析\r\n$P=UI$", cleaned["originalExplanation"])
        self.assertEqual(cleaned["originalExplanation"].count("\r\n"), marked["originalExplanation"].count("\r\n"))
        self.assertEqual(strip_personal_answers(cleaned), cleaned)

    def test_validator_rejects_non_string_optional_brief_explanation_without_mutation(self):
        for value in (None, 123, False, [], {}):
            malformed = bank([question()])
            malformed["questions"][0]["briefExplanation"] = value
            before = copy.deepcopy(malformed)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_bank(malformed)
            self.assertEqual(malformed, before)

    def test_validator_accepts_new_and_legacy_bank_without_answer_substitution(self):
        new = bank([question()])
        self.assertEqual(validate_bank(new), [])
        legacy = bank([question(correct=[])])
        legacy["questions"][0]["selectedAnswer"] = ["U=IR"]
        legacy["questions"][0]["rawSelectedAnswer"] = ["A"]
        before = copy.deepcopy(legacy)
        issues = validate_bank(legacy)
        self.assertIn("missing_correct_answer", {issue["code"] for issue in issues})
        self.assertEqual(legacy, before)
        sampled = select_sample(legacy, 10)
        self.assertEqual(strip_personal_answers(sampled), sampled)
        self.assertEqual(sampled["questions"][0]["correctAnswer"], [])

    def test_personal_field_filter_deep_copies_without_touching_answers(self):
        value = {"questions": [{"correctAnswer": ["正确答案"], "selectedAnswer": ["个人答案"],
                                "variants": [{"correctAnswer": ["另一个正确答案"],
                                              "rawSelectedAnswer": ["个人原始答案"]}]}]}
        before = copy.deepcopy(value)
        result = strip_personal_answers(value)
        self.assertEqual(result, {"questions": [{"correctAnswer": ["正确答案"],
                                                  "variants": [{"correctAnswer": ["另一个正确答案"]}]}]})
        result["questions"][0]["correctAnswer"].append("修改副本")
        self.assertEqual(value, before)

    def test_visual_boundary_empty_title_and_duplicates_are_unsafe_card_codes(self):
        required = {"requires_visual_review", "ambiguous_question_boundary", "numbered_field_continuation",
                    "duplicate_option_label", "empty_title", "duplicate_options", "invalid_answer_cardinality"}
        self.assertTrue(required.issubset(UNSAFE_CARD_CODES))
        q = bank([question(issues=[{"code": "requires_visual_review", "message": "扫描页需要核对"}])])["questions"][0]
        self.assertTrue(codes(q).intersection(UNSAFE_CARD_CODES))

    def test_direct_canonical_input_detects_bad_answer_count_blank_and_unmapped_answers(self):
        original = bank([question()])
        for kind, answers, expected in (
            ("单选", ["U=IR", "U=I/R"], "invalid_answer_cardinality"),
            ("判断", ["正确", "错误"], "invalid_answer_cardinality"),
            ("多选", ["U=IR", "U=IR"], "invalid_answer_cardinality"),
            ("单选", [" "], "missing_correct_answer"),
            ("单选", ["不属于选项的答案"], "unmapped_answer"),
        ):
            value = copy.deepcopy(original)
            value["questions"][0]["type"] = kind
            value["questions"][0]["correctAnswer"] = answers
            with self.subTest(kind=kind, answers=answers):
                reports = validate_bank(value)
                self.assertIn(expected, {issue["code"] for issue in reports})
                self.assertIn(expected, UNSAFE_CARD_CODES)

    def test_direct_canonical_malformed_topic_references_fail_explicitly(self):
        original = bank([question()])
        qid = original["questions"][0]["id"]
        topic = {"id": "t1", "title": "概念", "summary": "", "explanation": "",
                 "questionIds": [qid], "cards": []}
        value = copy.deepcopy(original)
        value["topics"] = [dict(topic, questionIds="not-an-array")]
        with self.assertRaises(ValueError):
            validate_bank(value)
        value["topics"] = [dict(topic, cards=[{"id": "c1", "prompt": "问题", "answer": "答案", "questionIds": []}])]
        reports = validate_bank(value)
        self.assertIn("unknown_question_reference", {issue["code"] for issue in reports})
        value["topics"][0]["cards"][0]["questionIds"] = None
        with self.assertRaises(ValueError):
            validate_bank(value)

    def test_sample_covers_all_types_formula_images_long_answers_and_anomalies(self):
        items = [question("单选", f"普通选择题 {i}") for i in range(12)]
        items.extend([
            question("多选", "U=IR 时哪些单位匹配？", ["AB"]),
            question("不定项", "任选哪些式子？", ["AB"]),
            question("判断", "判断导体", ["正确"], options=[]),
            question("填空", "图片里的单位是___", ["伏特"], options=[], images=["https://example.invalid/image.png"]),
            question("简答", "Explain the principle", ["A complete sentence. " * 20], options=[]),
            question("unknown", "未标注题型", [], options=[]),
        ])
        original = bank(items)
        qid = original["questions"][-2]["id"]
        original["questions"][-2]["topicIds"] = ["topic1"]
        original["topics"] = [{"id": "topic1", "title": "原理", "summary": "", "explanation": "",
                               "questionIds": [qid], "cards": [{"id": "card1", "prompt": "原理？", "answer": "A complete sentence.", "questionIds": [qid]}]}]
        sampled = select_sample(original, 10)
        self.assertEqual(len(sampled["questions"]), 10)
        self.assertEqual({q["type"] for q in sampled["questions"]}, {"单选", "多选", "不定项", "判断", "填空", "简答", "未知"})
        self.assertTrue(any(q["images"] for q in sampled["questions"]))
        self.assertTrue(any(any(len(a) >= 100 for a in q["correctAnswer"]) for q in sampled["questions"]))
        self.assertTrue(any(q["issues"] for q in sampled["questions"]))
        self.assertIn(qid, [q["id"] for q in sampled["questions"]])
        self.assertEqual(sampled["topics"][0]["questionIds"], [qid])
        self.assertEqual(select_sample(original, 10), sampled)
        self.assertEqual(len(original["questions"]), 18)
        self.assertTrue(validate_bank(sampled))

    def test_sample_limits_and_empty_banks(self):
        self.assertEqual(select_sample(bank([]))["questions"], [])
        self.assertEqual(select_sample(bank([question()]), 0)["questions"], [])
        self.assertEqual(len(select_sample(bank([question()]), 10)["questions"]), 1)
        for limit in (-1, 1.5, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                select_sample(bank([]), limit)

    def test_validation_reports_content_and_rejects_structure_without_mutation(self):
        original = bank([question(correct=[])])
        before = copy.deepcopy(original)
        reports = validate_bank(original)
        self.assertEqual(reports[0]["code"], "missing_correct_answer")
        self.assertEqual(reports[0]["questionId"], original["questions"][0]["id"])
        self.assertEqual(original, before)
        invalid = copy.deepcopy(original)
        invalid["questions"][0]["images"] = "bad"
        with self.assertRaises(ValueError):
            validate_bank(invalid)
        invalid = copy.deepcopy(original)
        invalid["questions"][0]["correctAnswer"] = [float("nan")]
        with self.assertRaises(ValueError):
            validate_bank(invalid)

    def test_sha256_matches_known_content(self):
        payload = b"source preservation\x00\xff"
        # Keep the temporary file in the workspace: this runtime's default
        # Windows sandbox temp directory does not permit opening child files.
        with tempfile.NamedTemporaryFile(dir=Path(__file__).parent, delete=False) as stream:
            stream.write(payload)
            path = Path(stream.name)
        try:
            self.assertEqual(file_sha256(path), hashlib.sha256(payload).hexdigest())
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
