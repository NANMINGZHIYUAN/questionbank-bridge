"""Fictional acceptance checks for the official four-field QA JSON text."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from test_word_export import _bank, _png, _question


class FojiaoQaExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from questionbank.fojiao_qa import export_qa_json
        cls.export_qa_json = staticmethod(export_qa_json)
        cls.pandoc = shutil.which("pandoc")
        if not cls.pandoc:
            raise unittest.SkipTest("Pandoc 未安装")

    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="fojiao-qa-", dir=base)
        self.root = Path(self.temp.name)
        self.export_count = 0

    def tearDown(self):
        self.temp.cleanup()

    def run_export(self, questions, *, pending=(), pending_details=None, assets=None):
        self.export_count += 1
        out = self.root / ("导出 含空格-" + str(self.export_count))
        out.mkdir()
        for filename, content in (assets or {}).items():
            target = out / "assets" / filename
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(content)
        bank = _bank(questions)
        original = copy.deepcopy(bank)
        word = {"pendingQuestionIds": list(pending), "qaMode": "manual",
                "wordQuestions": 0, "manualQuestions": len(questions) - len(pending),
                "pendingQuestions": len(pending)}
        if pending_details is not None:
            word["pending"] = copy.deepcopy(pending_details)
        report = {"files": {}, "warnings": [], "wordExport": copy.deepcopy(word)}
        self.export_qa_json(bank, out, str(self.pandoc), report)
        self.assertEqual(bank, original, "导出不能修改统一题库或来源")
        self.assertEqual(report["wordExport"], word, "新渠道不能重复改动 Word 三分类")
        self.assert_report(report, questions)
        rows = []
        if report["files"].get("fojiaoQaText"):
            path = Path(report["files"]["fojiaoQaText"])
            content = path.read_bytes()
            self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
            text = content.decode("utf-8")
            self.assertTrue(text.lstrip().startswith("["))
            self.assertTrue(text.rstrip().endswith("]"))
            rows = json.loads(text)
            for row in rows:
                self.assertEqual(set(row), {"题型", "题干", "答案", "解析"})
                self.assertEqual(row["题型"], "问答题")
                self.assertTrue(all(isinstance(value, str) for value in row.values()))
            self.assertEqual(len(rows), report["fojiaoQaExport"]["exportedQuestions"])
        return out, report, rows

    def assert_report(self, report, questions):
        qa = report["fojiaoQaExport"]
        selected = [q["id"] for q in questions if q["type"] == "简答"]
        self.assertEqual(qa["selectedQuestionIds"], selected)
        self.assertEqual(qa["selectedQuestions"], len(selected))
        exported, deferred = qa["exportedQuestionIds"], qa["deferredQuestionIds"]
        self.assertCountEqual(exported + deferred, selected)
        self.assertEqual(len(exported + deferred), len(set(exported + deferred)))
        self.assertEqual(qa["exportedQuestions"], len(exported))
        self.assertEqual(qa["deferredQuestions"], len(deferred))
        self.assertEqual(qa["selectedQuestions"], qa["exportedQuestions"] + qa["deferredQuestions"])
        self.assertCountEqual([item["questionId"] for item in qa["deferred"]], deferred)
        for item in qa["deferred"]:
            self.assertTrue(item["reasons"])
        self.assertFalse(qa["platformImportVerified"])
        self.assertFalse(qa["platformTextLimitVerified"])
        self.assertCountEqual([item["questionId"] for part in qa["parts"]
                               for item in part["questions"]], exported)
        for part in qa["parts"]:
            path = Path(part["file"])
            self.assertTrue(path.is_absolute())
            raw = path.read_bytes()
            self.assertEqual(part["bytes"], len(raw))
            self.assertEqual(part["characters"], len(raw.decode("utf-8")))
            self.assertEqual([item["number"] for item in part["questions"]],
                             list(range(1, len(part["questions"]) + 1)))
        if exported:
            self.assertEqual(len(qa["parts"]), 1, "未证实 JSON 上限时不得套用 Word 分包限制")
            self.assertEqual(report["files"]["fojiaoQaText"], qa["parts"][0]["file"])
        else:
            self.assertEqual(qa["parts"], [])
            self.assertNotIn("fojiaoQaText", report["files"])
        for warning in qa["warnings"]:
            self.assertIn(warning["questionId"], selected)
            self.assertTrue(warning["field"])
            self.assertTrue(warning["message"])

    def test_only_canonical_qa_is_exported_with_explicit_type_even_for_short_answers(self):
        questions = [_question("q-choice"), _question("q-judgment", "判断", ["正确"]),
                     _question("q-letter", "简答", ["A"], title="问答的答案恰好是字母。"),
                     _question("q-truth", "简答", ["正确"], title="问答的答案恰好是判断词。")]
        _, report, rows = self.run_export(questions)
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], ["q-letter", "q-truth"])
        self.assertEqual([row["答案"] for row in rows], ["A", "正确"])

    def test_case_multiline_answer_and_json_escaping_keep_complete_content(self):
        question = _question(
            "q-case-json", "简答",
            ['第一段保留引号 "电阻" 与代码 `print("保留双引号")`。\n第二行继续。',
             '第二段保留字面代码 `\\alpha` 和反斜杠。\n末行完整。'],
            title="解释测量现象。\n\n全部条件均需保留。",
            case="案例第一段事实。\n\n案例第二段事实。")
        _, report, rows = self.run_export([question])
        self.assertEqual(report["fojiaoQaExport"]["caseInStemIds"], [question["id"]])
        row = rows[0]
        self.assertTrue(row["题干"].startswith("案例"))
        for retained in ["案例第一段事实", "案例第二段事实", "解释测量现象", "全部条件均需保留"]:
            self.assertIn(retained, row["题干"])
        self.assertLess(row["题干"].index("案例第二段事实"), row["题干"].index("解释测量现象"))
        for retained in ["第一段保留引号", "电阻", 'print("保留双引号")', "第二行继续",
                         "第二段保留字面代码", r"\alpha", "末行完整"]:
            self.assertIn(retained, row["答案"])
        self.assertIn("\n", row["答案"])
        self.assertNotIn("案例", set(row), "不得添加未经官方格式确认的案例键")

    def test_actual_pandoc_formula_table_and_steps_become_readable_text(self):
        question = _question(
            "q-readable", "简答",
            ["1. 先测电流。\n\n2. 用公式 $U=IR$ 计算。\n\n"
             "| 检查项目 | 结果 |\n| --- | --- |\n| 接线 | 已检查 |\n\n3. 最后核对。"],
            title="把已核对的公式、表格和步骤录入。")
        _, _, rows = self.run_export([question])
        answer = rows[0]["答案"]
        for retained in ["先测电流", "检查项目", "结果", "接线", "已检查", "最后核对"]:
            self.assertIn(retained, answer)
        self.assertIn("U=IR", answer.replace(" ", ""))
        self.assertNotIn("$U=IR$", answer)
        self.assertNotIn("| --- |", answer)
        self.assertLess(answer.index("先测电流"), answer.index("最后核对"))

    def test_unknown_math_is_preserved_and_warned_without_excluding_the_qa(self):
        formula = r"\notacommand{x}"
        question = _question("q-unknown", "简答", ["原式 $" + formula + "$。\n后续步骤完整。"])
        _, report, rows = self.run_export([question])
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], [question["id"]])
        self.assertIn(formula, rows[0]["答案"])
        self.assertIn("后续步骤完整", rows[0]["答案"])
        self.assertTrue(any(warning["questionId"] == question["id"] and warning["field"] == "答案"
                            for warning in report["fojiaoQaExport"]["warnings"]))

    def test_html_personal_answers_and_private_source_details_do_not_enter_json_fields(self):
        question = _question(
            "q-private", "简答",
            ["<p>官方完整答案保留。</p><p>我的答案：JSON个人答案秘密</p><p>JSON个人续行秘密</p>"],
            title="<p>正式题干保留。</p><p>参考 https://mooc1.chaoxing.com/exam/private?secret=1</p>"
                  "<p>原件 D:/private/source.json</p><p>我的答案：JSON个人题干秘密</p>",
            case="<p>案例事实保留。</p><p>我的答案：JSON个人案例秘密</p>",
            briefExplanation="<p>可靠简析保留。</p><p>我的答案：JSON个人简析秘密</p>",
            selectedAnswer=["JSON个人字段秘密"])
        _, _, rows = self.run_export([question])
        visible = json.dumps(rows, ensure_ascii=False)
        for retained in ["正式题干保留", "案例事实保留", "官方完整答案保留", "可靠简析保留"]:
            self.assertIn(retained, visible)
        for private in ["JSON个人答案秘密", "JSON个人续行秘密", "JSON个人题干秘密", "JSON个人案例秘密",
                        "JSON个人简析秘密", "JSON个人字段秘密", "D:/private", "chaoxing.com",
                        "src-fiction", "sourceId", "selectedAnswer"]:
            self.assertNotIn(private, visible)

    def test_absent_brief_uses_reliable_explanation_or_empty_without_excluding(self):
        known = _question("q-fallback", "简答", ["已确认答案。"], title="可靠解析回退。",
                          originalExplanation="可靠原解析第一段。", explanation="有依据补充第二段。")
        known.pop("briefExplanation")
        empty = _question("q-empty-brief", "简答", ["另一份确定答案。"], title="无解析仍导出。",
                          briefExplanation="", originalExplanation="", explanation="")
        _, report, rows = self.run_export([known, empty])
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestions"], 2)
        self.assertIn("可靠原解析第一段", rows[0]["解析"])
        self.assertIn("有依据补充第二段", rows[0]["解析"])
        self.assertEqual(rows[1]["解析"], "")

    def test_content_pending_qa_is_deferred_while_other_qa_keeps_exporting(self):
        questions = [_question("q-pending", "简答", ["尚待核实的候选答案。"]),
                     _question("q-safe", "简答", ["已确认的完整答案。"], title="其它安全题继续。")]
        _, report, rows = self.run_export(questions, pending=["q-pending"])
        self.assertEqual(report["fojiaoQaExport"]["deferredQuestionIds"], ["q-pending"])
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], ["q-safe"])
        self.assertEqual([row["答案"] for row in rows], ["已确认的完整答案。"])

    def test_required_image_is_deferred_even_if_readable_and_source_material_stays(self):
        content, filename = _png((40, 90, 150)), "critical.png"
        question = _question("q-required", "简答", ["须结合关键图回答。"],
                             images=[{"path": "assets/" + filename, "field": "answer", "required": True}])
        out, report, rows = self.run_export([question], assets={filename: content})
        self.assertEqual(rows, [])
        self.assertEqual(report["fojiaoQaExport"]["deferredQuestionIds"], [question["id"]])
        self.assertEqual((out / "assets" / filename).read_bytes(), content)

    def test_required_inline_image_is_deferred_without_pasting_attachment_path(self):
        question = _question("q-inline", "简答", ["按图计算。\n\n![关键图](assets/critical.png)"],
                             images=[{"path": "assets/critical.png", "field": "answer", "required": True}])
        _, report, rows = self.run_export([question], assets={"critical.png": _png()})
        self.assertEqual(rows, [])
        self.assertEqual(report["fojiaoQaExport"]["deferredQuestionIds"], [question["id"]])

    def test_undeclared_inline_image_is_deferred_but_image_syntax_in_code_is_literal(self):
        questions = [
            _question("q-unregistered-inline", "简答", ["必须看图 ![题图](assets/unregistered.png) 才能回答。"]),
            _question("q-inline-code", "简答", ["示例代码 `![示例](assets/not-real.png)` 是文字。"]),
            _question("q-fenced-code", "简答", ["代码块示例：\n\n```text\n![示例](assets/not-real.png)\n```"]),
        ]
        _, report, rows = self.run_export(questions)
        self.assertEqual(report["fojiaoQaExport"]["deferredQuestionIds"], ["q-unregistered-inline"])
        self.assertIn("undeclared_inline_media", report["fojiaoQaExport"]["deferred"][0]["reasons"])
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], ["q-inline-code", "q-fenced-code"])
        for row in rows:
            self.assertIn("![示例](assets/not-real.png)", row["答案"])
        self.assertFalse(any(warning["questionId"] in {"q-inline-code", "q-fenced-code"}
                             and "图片" in warning["message"] and "附件" in warning["message"]
                             for warning in report["fojiaoQaExport"]["warnings"]))

    def test_unusual_qa_options_keep_original_labels_in_stem_without_changing_type(self):
        question = _question("q-qa-options", "简答", ["需要逐项解释理由。"],
                             title="解释给定项目。", options=[
                                 {"label": "甲", "text": "第一项目的完整内容"},
                                 {"label": "乙", "text": "第二项目的完整内容"}])
        _, _, rows = self.run_export([question])
        self.assertEqual(rows[0]["题型"], "问答题")
        for retained in ["甲", "第一项目的完整内容", "乙", "第二项目的完整内容"]:
            self.assertIn(retained, rows[0]["题干"])
        self.assertEqual(rows[0]["答案"], "需要逐项解释理由。")

    def test_html_personal_option_is_removed_before_qa_option_numbering(self):
        question = _question("q-qa-private-option", "简答", ["官方答案可靠。"], title="原题干可靠。",
                             options=[{"label": "A", "text": "<span>我的答案：PRIVATE_OPTION_SECRET</span>"},
                                      {"label": "B", "text": "可靠附带条件保留。"}])
        _, _, rows = self.run_export([question])
        self.assertEqual(rows[0]["题型"], "问答题")
        self.assertIn("原题干可靠", rows[0]["题干"])
        self.assertIn("可靠附带条件保留", rows[0]["题干"])
        self.assertNotIn("PRIVATE_OPTION_SECRET", json.dumps(rows, ensure_ascii=False))
        self.assertNotIn("我的答案", rows[0]["题干"])

    def test_private_only_upload_fields_are_deferred_after_html_cleanup(self):
        questions = [
            _question("q-empty-title", "简答", ["可靠答案。"], title="<p>我的答案：PRIVATE_STEM</p>"),
            _question("q-empty-answer", "简答", ["<p>我的答案：PRIVATE_ANSWER</p>"], title="可靠题干。"),
        ]
        _, report, rows = self.run_export(questions)
        self.assertEqual(rows, [])
        self.assertCountEqual(report["fojiaoQaExport"]["deferredQuestionIds"], [q["id"] for q in questions])
        for deferred in report["fojiaoQaExport"]["deferred"]:
            self.assertIn("empty_upload_field_after_cleanup", deferred["reasons"])

    def test_specific_word_pending_reasons_are_preserved_in_json_channel_report(self):
        question = _question("q-pending-detail", "简答", ["两个来源尚未核对。"])
        reasons = ["conflicting_correct_answers", "requires_content_review"]
        _, report, rows = self.run_export([question], pending=[question["id"]],
                                         pending_details=[{"questionId": question["id"], "reasons": reasons}])
        self.assertEqual(rows, [])
        self.assertCountEqual(report["fojiaoQaExport"]["deferred"][0]["reasons"], reasons)

    def test_optional_media_keeps_reliable_text_and_warns_without_whole_type_exclusion(self):
        filename = "optional.png"
        question = _question("q-optional", "简答", ["确定答案完整保留。"],
                             title="文字条件足够，附件只是示意。\n\n![非关键示意](assets/optional.png)",
                             images=[{"path": "assets/" + filename, "field": "title", "required": False}])
        out, report, rows = self.run_export([question], assets={filename: _png()})
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], [question["id"]])
        self.assertEqual(rows[0]["答案"], "确定答案完整保留。")
        self.assertNotIn("assets/", json.dumps(rows, ensure_ascii=False))
        self.assertTrue(any(warning["questionId"] == question["id"]
                            for warning in report["fojiaoQaExport"]["warnings"]))
        self.assertTrue((out / "assets" / filename).is_file())

    def test_optional_other_image_in_same_field_cannot_approve_new_inline_image(self):
        question = _question("q-unrelated-image", "简答", ["可靠文字。\n\n![新图](assets/critical-new.png)"],
                             images=[{"path": "assets/optional-other.png", "field": "answer", "required": False}])
        _, report, rows = self.run_export([question])
        self.assertEqual(rows, [])
        self.assertEqual(report["fojiaoQaExport"]["deferredQuestionIds"], [question["id"]])
        self.assertIn("undeclared_inline_media", report["fojiaoQaExport"]["deferred"][0]["reasons"])

    def test_legacy_optional_image_in_answer_exports_with_position_warning(self):
        question = _question("q-legacy-position", "简答", ["可靠完整答案。\n\n![旧图](assets/optional.png)"],
                             images=[{"path": "assets/optional.png", "required": False}])
        _, report, rows = self.run_export([question])
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], [question["id"]])
        self.assertIn("可靠完整答案", rows[0]["答案"])
        self.assertNotIn("assets/optional.png", rows[0]["答案"])
        self.assertTrue(any(warning["questionId"] == question["id"]
                            and any(word in warning["message"] for word in ["位置", "定位", "标注"])
                            for warning in report["fojiaoQaExport"]["warnings"]))

    def test_repeated_inline_reference_does_not_consume_optional_image_metadata(self):
        question = _question(
            "q-repeat-image", "简答",
            ["第一段可靠文字。\n\n![第一次引用](assets/repeated.png)\n\n"
             "第二段可靠文字。\n\n![第二次引用](assets/repeated.png)"],
            images=[{"path": "assets/repeated.png", "field": "answer", "required": False}])
        _, report, rows = self.run_export([question])
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], [question["id"]])
        self.assertFalse(report["fojiaoQaExport"]["deferredQuestionIds"])
        for retained in ["第一段可靠文字", "第二段可靠文字"]:
            self.assertIn(retained, rows[0]["答案"])

    def test_same_basename_in_different_local_or_url_directories_is_not_same_media(self):
        identities = [
            ({"path": "registered/shared.png"}, "unregistered/shared.png"),
            ({"url": "https://example.invalid/registered/shared.png"},
             "https://example.invalid/unregistered/shared.png"),
        ]
        for index, (identity, inline) in enumerate(identities):
            with self.subTest(identity=identity):
                question = _question("q-distinct-location-" + str(index), "简答",
                                     ["实际依赖另一张图。\n\n![题图](" + inline + ")"],
                                     images=[dict(identity, field="answer", required=False)])
                _, report, rows = self.run_export([question])
                self.assertEqual(rows, [])
                self.assertIn("undeclared_inline_media", report["fojiaoQaExport"]["deferred"][0]["reasons"])

    def test_optional_placement_only_issue_does_not_block_reliable_qa_text_or_change_word_route(self):
        question = _question("q-position-only", "简答", ["可靠文字答案完整。"],
                             images=[{"path": "assets/optional.png", "field": "option",
                                      "optionIndex": 0, "required": False}])
        _, report, rows = self.run_export(
            [question], pending=[question["id"]], pending_details=[{
                "questionId": question["id"],
                "reasons": ["invalid_image_placement", "选项图片的位置索引无效，需回看原题。"]}])
        self.assertEqual(report["wordExport"]["pendingQuestionIds"], [question["id"]])
        self.assertEqual(report["wordExport"]["pendingQuestions"], 1)
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], [question["id"]])
        self.assertEqual(rows[0]["答案"], "可靠文字答案完整。")
        self.assertTrue(any(warning["questionId"] == question["id"]
                            and any(word in warning["message"] for word in ["位置", "定位", "标注"])
                            for warning in report["fojiaoQaExport"]["warnings"]))

        # Relaxing a placement-only gate must not relax a real content doubt.
        _, report, rows = self.run_export(
            [question], pending=[question["id"]], pending_details=[{
                "questionId": question["id"],
                "reasons": ["invalid_image_placement", "requires_content_review"]}])
        self.assertEqual(rows, [])
        self.assertEqual(report["fojiaoQaExport"]["deferredQuestionIds"], [question["id"]])
        self.assertIn("requires_content_review", report["fojiaoQaExport"]["deferred"][0]["reasons"])

    def test_escaped_image_and_image_syntax_inside_math_are_not_media_dependencies(self):
        questions = [
            _question("q-escaped-image", "简答", [r"示例写法 \![图片](sample.png) 是字面文本。"]),
            _question("q-math-image", "简答", [r"$\text{![图片](sample.png)}$ 只是数学里的文字。"]),
            _question("q-escaped-nested", "简答", [r"嵌套文件名 \![图片](sample(a).png) 完整保留。"]),
            _question("q-escaped-reference", "简答", [r"引用语法 \![图片][ref]" + "\n\n[ref]: sample.png"]),
        ]
        _, report, rows = self.run_export(questions)
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], [q["id"] for q in questions])
        self.assertFalse(report["fojiaoQaExport"]["deferred"])
        self.assertIn("示例写法", rows[0]["答案"])
        self.assertIn("只是数学里的文字", rows[1]["答案"])
        for row in rows[:2]:
            self.assertIn("![图片](sample.png)", row["答案"])
        self.assertIn("![图片](sample(a).png)", rows[2]["答案"])
        self.assertIn("![图片][ref]", rows[3]["答案"])
        self.assertFalse(any("图片" in warning["message"] and "附件" in warning["message"]
                             for warning in report["fojiaoQaExport"]["warnings"]))

    def test_escaped_literal_private_image_source_is_redacted_before_json_serialization(self):
        questions = [
            _question("q-escaped-private-file", "简答", [r"示例 \![图片](C:/private/PRIVATE_LITERAL_FILE.png) 可靠说明保留。"]),
            _question("q-escaped-private-url", "简答", [
                r"示例 \![图片](https://mooc1.chaoxing.com/exam?courseid=PRIVATE_LITERAL_URL) 可靠说明保留。"]),
        ]
        _, report, rows = self.run_export(questions)
        self.assertEqual(report["fojiaoQaExport"]["exportedQuestionIds"], [q["id"] for q in questions])
        self.assertFalse(report["fojiaoQaExport"]["deferred"])
        visible = json.dumps(rows, ensure_ascii=False)
        for private in ["C:/private", "PRIVATE_LITERAL_FILE", "chaoxing.com", "PRIVATE_LITERAL_URL", "courseid="]:
            self.assertNotIn(private, visible)
        for row in rows:
            self.assertIn("可靠说明保留", row["答案"])
            self.assertIn("省略", row["答案"])

    def test_long_answer_over_word_byte_limit_stays_complete_in_one_json_array(self):
        repeated = "完整长答案不会截断；"
        answer = "开头完整。" + repeated * 30000 + "结尾完整。"
        _, report, rows = self.run_export([_question("q-long", "简答", [answer])])
        self.assertEqual(rows[0]["答案"], answer)
        self.assertEqual(rows[0]["答案"].count(repeated), 30000)
        self.assertGreater(report["fojiaoQaExport"]["parts"][0]["bytes"], 500000)
        self.assertEqual(len(report["fojiaoQaExport"]["parts"]), 1)

    def test_without_qa_or_with_all_deferred_no_empty_json_file_is_generated(self):
        out, _, rows = self.run_export([_question("q-choice-only")])
        self.assertEqual(rows, [])
        self.assertFalse((out / "佛脚问答批量导入.txt").exists())
        out, _, rows = self.run_export([_question("q-all-pending", "简答", ["候选答案。"] )],
                                      pending=["q-all-pending"])
        self.assertEqual(rows, [])
        self.assertFalse((out / "佛脚问答批量导入.txt").exists())


if __name__ == "__main__":
    unittest.main()
