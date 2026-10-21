"""Acceptance checks for the upload Word and its local question-routing report.

Fixtures are fictional. DOCX contents and final ZIP sizes are checked with the
real project Pandoc; no miniature copy of the renderer is used in these tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile
import zlib


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _png(rgb=(255, 255, 255)):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb)))
            + chunk(b"IEND", b""))


def _question(qid, kind="单选", answer=None, **fields):
    choice = kind in {"单选", "多选", "不定项"}
    question = {
        "id": qid, "type": kind, "title": "电阻使用哪个单位？",
        "options": [{"label": "A", "text": "欧姆"},
                    {"label": "B", "text": "伏特"}] if choice else [],
        "correctAnswer": ["欧姆"] if answer is None else answer,
        "originalExplanation": "电阻的单位是欧姆。",
        "explanation": "", "briefExplanation": "电阻用欧姆，电压用伏特。",
        "topicIds": [], "images": [],
        "sources": [{"sourceId": "src-fiction", "locator": "题号=" + qid}],
        "issues": [],
    }
    question.update(fields)
    return question


def _bank(questions):
    return {
        "schemaVersion": 1, "course": "虚构导入验证课程",
        "sources": [{"id": "src-fiction", "path": "D:/private/source.json",
                     "sha256": "f" * 64, "format": "json",
                     "url": "https://mooc1.chaoxing.com/exam/private?secret=1"}],
        "questions": questions, "topics": [],
    }


def _document(path):
    with zipfile.ZipFile(path) as package:
        root = ET.fromstring(package.read("word/document.xml"))
        names = package.namelist()
    text = "\n".join("".join(paragraph.itertext())
                     for paragraph in root.findall(".//w:p", NS))
    return root, text, names


class WordExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from questionbank.word_export import export_word
        try:
            import docx  # noqa: F401
        except ImportError as error:
            raise unittest.SkipTest(f"Word 依赖未就绪：{error}")
        cls.export_word = staticmethod(export_word)
        cls.pandoc = shutil.which("pandoc")
        if not cls.pandoc:
            raise unittest.SkipTest("Pandoc 未安装")

    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="word-platform-", dir=base)
        self.root = Path(self.temp.name)
        self.export_count = 0

    def tearDown(self):
        self.temp.cleanup()

    def run_export(self, questions, *, mode="include", max_bytes=500000,
                   media=None, required_missing=None, assets=None):
        self.export_count += 1
        out = self.root / ("导出 含空格-" + str(self.export_count))
        out.mkdir()
        for filename, content in (assets or {}).items():
            target = out / "assets" / filename
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(content)
        bank = _bank(questions)
        original = copy.deepcopy(bank)
        report = {"files": {}, "warnings": []}
        options = {"max_bytes": max_bytes}
        if mode is not None:
            options["word_qa_mode"] = mode
        self.export_word(bank, out, str(self.pandoc), report, media or {},
                         set(required_missing or ()), **options)
        self.assertEqual(bank, original, "Word 导出不得改变统一题库")
        self.assert_routes(report, questions)
        return out, report

    def assert_routes(self, report, questions):
        routing = report["wordExport"]
        self.assertEqual(routing["selectedQuestions"], len(questions))
        self.assertEqual(routing["selectedQuestions"],
                         routing["wordQuestions"] + routing["manualQuestions"]
                         + routing["pendingQuestions"])
        groups = [routing["wordQuestionIds"], routing["manualQuestionIds"],
                  routing["pendingQuestionIds"]]
        routed = [qid for group in groups for qid in group]
        self.assertCountEqual(routed, [q["id"] for q in questions])
        self.assertEqual(len(set(routed)), len(routed), "同一道题不能进入两条路由")
        for count, ids in zip(("wordQuestions", "manualQuestions", "pendingQuestions"),
                              groups):
            self.assertEqual(routing[count], len(ids))
        parts = routing["parts"]
        self.assertEqual(report["files"]["questionDocxParts"],
                         [part["file"] for part in parts])
        self.assertCountEqual([q["questionId"] for part in parts
                               for q in part["questions"]], groups[0])
        if parts:
            self.assertEqual(report["files"]["questionDocx"], parts[0]["file"])
            for part in parts:
                path = Path(part["file"])
                self.assertTrue(path.is_absolute())
                self.assertEqual(part["bytes"], path.stat().st_size)
                self.assertTrue(zipfile.is_zipfile(path))
        else:
            self.assertFalse(report["files"].get("questionDocx"))
        for item in routing["pending"]:
            self.assertIn(item["questionId"], groups[2])
            self.assertTrue(item["reasons"])
            self.assertEqual(item["sources"], next(
                q["sources"] for q in questions if q["id"] == item["questionId"]))
            self.assertTrue(item["suggestions"], "待核实项须提供具体核实建议")
        backups = routing["backupManualQuestionIds"]
        self.assertEqual(len(backups), len(set(backups)))
        self.assertTrue(set(backups).issubset(set(routing["wordQuestionIds"])))
        if report["files"].get("manualQuestions"):
            self.assertEqual(Path(report["files"]["manualQuestions"]).suffix, ".txt")
            source = Path(report["files"]["manualQuestionsMarkdown"])
            self.assertEqual(source.suffix, ".md")
            self.assertTrue(source.is_file())
        for attachment in routing["mediaAttachments"]:
            self.assertFalse(attachment["platformDisplayVerified"])
        json.dumps(report, ensure_ascii=False)

    def test_real_docx_groups_types_renumbers_options_and_omits_internal_fields(self):
        questions = [
            _question("q-qa", "简答", ["单位电荷通过两点的电势能差。"], title="解释电压。"),
            _question("q-fill", "填空", ["1.5", "(1) 3.14"], title="填写电流及计算值。"),
            _question("q-judge", "判断", ["truth"], title="欧姆是电阻单位。",
                      options=[{"label": "truth", "text": "正确"},
                               {"label": "falsehood", "text": "错误"}]),
            _question("q-indefinite", "不定项", ["欧姆"], title="选择电阻单位。"),
            _question("q-multi", "多选", ["欧姆", "瓦特"], title="选择给定单位。",
                      options=[{"label": "甲", "text": "欧姆"},
                               {"label": "乙", "text": "伏特"},
                               {"label": "丙", "text": "瓦特"}]),
            _question("q-single", title="电阻单位题。", selectedAnswer=["PERSONAL-SECRET"],
                      rawSelectedAnswer=["PERSONAL-RAW"],
                      options=[{"label": "第一项", "text": "欧姆"},
                               {"label": "第二项", "text": "伏特"}]),
        ]
        _, report = self.run_export(questions)
        routing = report["wordExport"]
        self.assertEqual(routing["wordQuestions"], 6)
        self.assertEqual([q["type"] for part in routing["parts"] for q in part["questions"]],
                         ["单选", "多选", "不定项", "判断", "填空", "简答"])
        _, text, _ = _document(report["files"]["questionDocx"])
        for heading in ["单选题", "多选题", "不定项选择题", "判断题", "填空题", "问答题"]:
            self.assertIn(heading, text)
        self.assertIn("A. 欧姆", text)
        self.assertIn("B. 伏特", text)
        self.assertIn("C. 瓦特", text)
        self.assertIn("答案：AC", text)
        self.assertIn("答案：正确", text)
        self.assertIn("1.5", text)
        self.assertIn("(1) 3.14", text)
        for value in ["PERSONAL-SECRET", "PERSONAL-RAW", "src-fiction", "q-single",
                      "q-qa", "D:/private", "chaoxing.com", "知识点：", "核实："]:
            self.assertNotIn(value, text)

    def test_html_personal_answer_fields_are_absent_from_word_and_manual_materials(self):
        question = _question(
            "q-html-personal", "简答",
            ["<p>官方完整答案保留。</p><p>我的答案：HTML个人答案秘密</p>"
             "<p>HTML个人答案续行秘密</p>"],
            title="<p>HTML题干正文保留。</p><p>我的答案：HTML个人题干秘密</p>",
            case="<p>案例事实保留。</p><p>我的答案：HTML个人案例秘密</p>",
            briefExplanation="<p>可靠解析保留。</p><p>我的答案：HTML个人解析秘密</p>")
        _, report = self.run_export([question], mode="include")
        _, word_text, _ = _document(report["files"]["questionDocx"])
        plain = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        source = Path(report["files"]["manualQuestionsMarkdown"]).read_text(encoding="utf-8")
        self.assertEqual(report["wordExport"]["wordQuestions"], 1)
        self.assertEqual(report["wordExport"]["backupManualQuestionIds"], [question["id"]])
        for material in [word_text, plain, source]:
            for retained in ["HTML题干正文保留", "案例事实保留", "官方完整答案保留", "可靠解析保留"]:
                self.assertIn(retained, material)
            for private in ["HTML个人答案秘密", "HTML个人答案续行秘密", "HTML个人题干秘密",
                            "HTML个人案例秘密", "HTML个人解析秘密"]:
                self.assertNotIn(private, material)

        question['correctAnswer'] = ['<table><tr><td>普通名称保留。</td>'
            '<td>我的答案：HTML单元格秘密</td><td>正确答案：官方值保留。</td>'
            '<td>必要步骤保留。</td></tr></table>']
        _, table_report = self.run_export([question], mode='include')
        _, table_word, _ = _document(table_report['files']['questionDocx'])
        materials = [table_word, Path(table_report['files']['manualQuestions']).read_text(encoding='utf-8'),
                     Path(table_report['files']['manualQuestionsMarkdown']).read_text(encoding='utf-8')]
        for material in materials:
            self.assertNotIn('HTML单元格秘密', material)
            for retained in ['普通名称保留', '官方值保留', '必要步骤保留']:
                self.assertIn(retained, material)

    def test_include_keeps_complete_qa_and_backup_without_double_counting(self):
        answer = ("1. 首先断开电源。\n\n2. 然后检查接线，公式 $U=IR$。\n\n"
                  "| 检查项目 | 结果 |\n| --- | --- |\n| 接线 | 已检查 |\n\n"
                  + "有依据的完整步骤；" * 60 + "最后恢复并确认运行。")
        questions = [_question("q-case", "简答", [answer],
                               title="案例：测量异常，怎样排查？\n保留第二段条件。")]
        _, report = self.run_export(questions)
        self.assertEqual(report["wordExport"]["manualQuestions"], 0)
        self.assertEqual(report["wordExport"]["wordQuestions"], 1)
        root, text, _ = _document(report["files"]["questionDocx"])
        for value in ["案例：测量异常", "保留第二段条件", "首先断开电源", "然后检查接线",
                      "已检查", "最后恢复并确认运行"]:
            self.assertIn(value, text)
        self.assertGreaterEqual(text.count("有依据的完整步骤"), 60)
        self.assertTrue(root.findall(".//m:oMath", NS))
        self.assertTrue(root.findall(".//w:tbl", NS))
        backup = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        source = Path(report["files"]["manualQuestionsMarkdown"]).read_text(encoding="utf-8")
        self.assertIn(answer, source)
        self.assertIn("案例：测量异常", backup)
        self.assertIn("首先断开电源", backup)
        self.assertIn("然后检查接线", backup)
        self.assertIn("最后恢复并确认运行", backup)
        self.assertGreaterEqual(backup.count("有依据的完整步骤"), 60)
        for value in ["检查项目", "结果", "接线", "已检查"]:
            self.assertIn(value, backup)
        self.assertNotIn("| --- |", backup)
        self.assertNotIn("$U=IR$", backup)
        self.assertNotIn("### 答案", backup)
        self.assertIn("【答案】", backup)

    def test_multiple_underscore_blanks_preserve_real_word_math_code_and_link(self):
        question = _question("q-blank-underscores", "填空", ["安培", "伏特"],
                             title="第一空____，第二空____。公式 $x_1+y_2=z_3$，"
                                   "代码 `name__value`。参考[参数](https://example.org/name__value)。")
        _, report = self.run_export([question])
        root, text, _ = _document(report["files"]["questionDocx"])
        self.assertIn("第一空____", text)
        self.assertIn("第二空____", text)
        self.assertIn("name__value", text)
        self.assertNotIn(r"\_", text)
        self.assertGreaterEqual(len(root.findall(".//m:sSub", NS)), 3,
                                "公式中的下划线须仍被渲染为数学下标")
        self.assertTrue(root.findall(".//m:oMath", NS))
        with zipfile.ZipFile(report["files"]["questionDocx"]) as package:
            relationships = ET.fromstring(package.read("word/_rels/document.xml.rels"))
        self.assertTrue(any(item.get("Target") == "https://example.org/name__value"
                            for item in relationships))

    def test_manual_routes_only_safe_qa_and_retains_all_three_fields(self):
        full = "第一段答案。\n\n1. 保留步骤一。\n2. 保留步骤二。\n" + "完整内容。" * 80
        questions = [
            _question("q-choice", title="可上传选择题。"),
            _question("q-qa", "简答", [full], title="安全问答的题干。",
                      briefExplanation="先断电再核查，记住检查顺序。"),
            _question("q-unresolved", "简答", ["不可当作确定答案。"], title="待核实问答。",
                      issues=[{"code": "requires_content_review", "message": "确认原答案。"}]),
        ]
        _, report = self.run_export(questions, mode="manual")
        routing = report["wordExport"]
        self.assertEqual(routing["wordQuestionIds"], ["q-choice"])
        self.assertEqual(routing["manualQuestionIds"], ["q-qa"])
        self.assertEqual(routing["pendingQuestionIds"], ["q-unresolved"])
        _, uploaded, _ = _document(report["files"]["questionDocx"])
        self.assertNotIn("安全问答", uploaded)
        self.assertNotIn("待核实问答", uploaded)
        self.assertNotIn("待核实", uploaded)
        manual = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        for value in ["【题干】", "【答案】", "【解析】", "安全问答的题干。",
                      "第一段答案。", "保留步骤一。", "保留步骤二。",
                      "先断电再核查，记住检查顺序。"]:
            self.assertIn(value, manual)
        self.assertGreaterEqual(manual.count("完整内容。"), 80)
        source = Path(report["files"]["manualQuestionsMarkdown"]).read_text(encoding="utf-8")
        self.assertIn(full, source)
        self.assertNotIn("待核实问答", manual)
        self.assertNotIn("不可当作确定答案", manual)

    def test_default_api_routes_qa_to_plain_text_and_retains_original_case(self):
        question = _question("q-default-qa", "简答", ["1. 断电。\n2. 按原图检查。"],
                             title="默认问答应手动录入。", case="某装置测量异常，需检查接线。")
        _, report = self.run_export([question], mode=None)
        word = report["wordExport"]
        self.assertEqual(word["qaMode"], "manual")
        self.assertEqual(word["manualQuestionIds"], [question["id"]])
        self.assertEqual(word["wordQuestions"], 0)
        plain = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        for value in ["【案例】", "【题干】", "【答案】", "【解析】", question["case"],
                      question["title"], "断电。", "按原图检查。"]:
            self.assertIn(value, plain)
        self.assertNotIn("### ", plain)

    def test_unknown_tex_warns_and_preserves_source_in_manual_material(self):
        answer = r"未知公式 $\notacommand{x}$ 必须保留原式。完整操作步骤不能删去。"
        question = _question("q-unknown-tex", "简答", [answer], title="未知公式纯文本回退。")
        _, report = self.run_export([question], mode="manual")
        self.assertEqual(report["wordExport"]["pendingQuestions"], 0)
        warnings = report["wordExport"]["manualTextWarnings"]
        self.assertTrue(any(item["questionId"] == question["id"] and item["field"] == "答案"
                            and item["message"] for item in warnings))
        plain = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        source = Path(report["files"]["manualQuestionsMarkdown"]).read_text(encoding="utf-8")
        self.assertIn(r"\notacommand{x}", plain)
        self.assertIn("完整操作步骤不能删去", plain)
        self.assertIn(answer, source)
        self.assertIn("【答案格式需核对，不粘贴到文字框】", plain)
        copy_answer = plain.split("【答案】\n", 1)[1].split("\n【", 1)[0]
        self.assertIn(r"\notacommand{x}", copy_answer)
        for item in warnings:
            if item["field"] == "答案":
                self.assertIn(item["message"], plain)
                self.assertNotIn(item["message"], copy_answer)

    def test_all_manual_produces_no_empty_upload_word(self):
        _, report = self.run_export([
            _question("q-only-manual", "简答", ["完整答案。"], title="仅有问答。")], mode="manual")
        self.assertEqual(report["wordExport"]["manualQuestions"], 1)
        self.assertEqual(report["files"]["questionDocxParts"], [])
        self.assertTrue(Path(report["files"]["manualQuestions"]).is_file())

    def test_all_pending_produces_report_without_empty_upload_word(self):
        _, report = self.run_export([
            _question("q-missing", answer=[], title="没有正确答案的题。"),
            _question("q-content", issues=[{"code": "requires_content_review",
                                            "message": "核实选项含义。"}])])
        self.assertEqual(report["wordExport"]["pendingQuestions"], 2)
        self.assertEqual(report["files"]["questionDocxParts"], [])

    def test_answer_text_beats_conflicting_legacy_label_and_duplicate_text_is_pending(self):
        questions = [
            _question("q-text-collision", answer=["B"], title="答案文本与别项标签重合。",
                      options=[{"label": "A", "text": "B"}, {"label": "B", "text": "C"}]),
            _question("q-duplicate", answer=["相同内容"], title="重复内容不能唯一映射。",
                      options=[{"label": "甲", "text": "相同内容"},
                               {"label": "乙", "text": "相同内容"}]),
            _question("q-multi-invalid", "多选", ["欧姆", "不存在的选项"], title="无效多选。"),
            _question("q-double-single", answer=["欧姆", "伏特"], title="单选却有双答案。"),
        ]
        _, report = self.run_export(questions)
        self.assertEqual(report["wordExport"]["wordQuestionIds"], ["q-text-collision"])
        self.assertEqual(report["wordExport"]["pendingQuestions"], 3)
        _, text, _ = _document(report["files"]["questionDocx"])
        self.assertIn("答案：A", text)
        self.assertNotIn("重复内容不能唯一映射", text)

    def test_verified_27_options_route_to_complete_manual_without_content_approval_gate(self):
        options = [{"label": f"原选项{index}", "text": f"第{index}项的独立内容"}
                   for index in range(1, 28)]
        question = _question("q-27-options", answer=[options[-1]["text"]],
                             title="平台字母容纳不下，但来源内容已经核实。", options=options)
        _, report = self.run_export([question])
        word = report["wordExport"]
        self.assertEqual(word["manualQuestionIds"], [question["id"]])
        self.assertEqual(word["formatManualQuestions"], 1)
        self.assertEqual(word["pendingQuestions"], 0)
        self.assertEqual(word["pending"], [])
        self.assertEqual(word["wordQuestions"], 0)
        for item in word["formatManual"]:
            self.assertFalse(item.get("requiresUserApprovalBeforeSupplement"))
        manual = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        for option in options:
            self.assertIn(option["text"], manual)
        self.assertIn("原选项27", manual)
        self.assertIn("【答案】\n" + options[-1]["text"], manual)
        self.assertIn(question["briefExplanation"], manual)

    def test_27_legacy_labels_keep_manual_option_identity_and_complete_answer_text(self):
        options = [{"label": f"L{index}", "text": f"第{index}项的完整独立内容"}
                   for index in range(1, 28)]
        question = _question("q-27-legacy", answer=["L1"], options=options,
                             title="旧标签答案的纯格式手动回退。")
        _, report = self.run_export([question])
        word = report["wordExport"]
        self.assertEqual(word["manualQuestionIds"], [question["id"]])
        self.assertEqual(word["formatManualQuestions"], 1)
        self.assertEqual(word["pendingQuestions"], 0)
        self.assertFalse(word["pending"])
        plain = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        source = Path(report["files"]["manualQuestionsMarkdown"]).read_text(encoding="utf-8")
        for option in options:
            self.assertIn("【选项" + option["label"] + "】", plain)
            self.assertIn(option["label"] + r"\. " + option["text"], source)
        self.assertIn("【答案】\n" + options[0]["text"], plain)
        self.assertIn("### 答案\n\n" + options[0]["text"], source)
        self.assertNotIn("【答案】\nL1\n", plain)
        self.assertNotIn("### 答案\n\nL1\n", source)

    def test_judgment_label_resolves_to_false_semantics(self):
        _, report = self.run_export([
            _question("q-false", "判断", ["N"], title="伏特是电阻单位。",
                      options=[{"label": "Y", "text": "对"}, {"label": "N", "text": "错"}])])
        _, text, _ = _document(report["files"]["questionDocx"])
        self.assertIn("答案：错误", text)
        self.assertNotIn("答案：N", text)

    def test_missing_required_image_blocks_but_legacy_local_image_is_kept(self):
        content = _png()
        filename = hashlib.sha256(content).hexdigest() + ".png"
        questions = [
            _question("q-required", title="关键题图缺失。",
                      images=[{"url": "https://example.invalid/required.png", "required": True}]),
            _question("q-local", title="已有本地旧题图。",
                      images=[{"path": "assets/" + filename, "required": True}]),
            _question("q-optional", title="非关键图可以省略。",
                      images=[{"url": "https://example.invalid/optional.png", "required": False}]),
        ]
        _, report = self.run_export(questions, media={"q-local": [filename]},
                                    required_missing={"q-required"}, assets={filename: content})
        self.assertEqual(report["wordExport"]["pendingQuestionIds"], ["q-required"])
        root, text, names = _document(report["files"]["questionDocx"])
        self.assertIn("已有本地旧题图", text)
        self.assertIn("非关键图可以省略", text)
        self.assertNotIn("关键题图缺失", text)
        self.assertTrue(root.findall(".//a:blip", NS))
        self.assertTrue(any(name.startswith("word/media/") for name in names))
        self.assertTrue(report["wordExport"]["warnings"], "旧图未标位置须明确提示")
        self.assertEqual(report["wordExport"]["mediaProblems"], [], "可解码PNG不应产生媒体故障")

    def test_legacy_media_without_image_metadata_has_manual_attachment_and_position_warning(self):
        question = _question("q-legacy-media", title="旧调用提供题图，但没有图片元数据。")
        filename = "legacy.png"
        _, report = self.run_export([question], media={question["id"]: [filename]},
                                    assets={filename: _png((20, 80, 160))})
        word = report["wordExport"]
        self.assertEqual(word["wordQuestions"], 1)
        self.assertEqual(word["manualQuestions"], 0)
        self.assertEqual(word["pendingQuestions"], 0)
        self.assertEqual(word["backupManualQuestionIds"], [question["id"]])
        plain = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        source = Path(report["files"]["manualQuestionsMarkdown"]).read_text(encoding="utf-8")
        for material in [plain, source]:
            self.assertIn("assets/" + filename, material)
        self.assertIn("题干后：assets/" + filename, plain)
        self.assertIn({"questionId": question["id"], "field": "title", "file": "assets/" + filename,
                       "required": False, "platformDisplayVerified": False,
                       "placementNeedsReview": True}, word["mediaAttachments"])
        self.assertTrue(any(question["id"] in warning and "无位置标注" in warning and "题干" in warning
                            for warning in word["warnings"]))
        root, _, _ = _document(report["files"]["questionDocx"])
        self.assertEqual(len(root.findall(".//a:blip", NS)), 1)

    def test_corrupt_required_png_is_pending_and_other_question_still_exports(self):
        # Keep the PNG signature and IHDR, but truncate compressed pixel data.
        # Existence and a plausible extension/header alone must not pass media.
        content = _png()[:50]
        filename = "truncated-required.png"
        questions = [
            _question("q-corrupt-required", title="需要损坏图片才能理解的题。",
                      images=[{"path": "assets/" + filename, "required": True, "field": "title"}]),
            _question("q-independent", title="无图的其余安全题照常导出。"),
        ]
        out, report = self.run_export(questions, media={"q-corrupt-required": [filename]},
                                      assets={filename: content})
        word = report["wordExport"]
        self.assertEqual(word["pendingQuestionIds"], ["q-corrupt-required"])
        self.assertEqual(word["wordQuestionIds"], ["q-independent"])
        self.assertEqual(word["manualQuestionIds"], [])
        self.assertEqual(word["mediaProblems"], [{
            "questionId": "q-corrupt-required", "file": "assets/" + filename,
            "reason": "unreadable_media", "required": True,
        }])
        self.assertIn("invalid_required_image", word["pending"][0]["reasons"])
        _, text, _ = _document(report["files"]["questionDocx"])
        self.assertIn("其余安全题照常导出", text)
        self.assertNotIn("需要损坏图片", text)
        self.assertEqual((out / "assets" / filename).read_bytes(), content)

    def test_corrupt_optional_png_warns_and_keeps_word_with_attachment_backup(self):
        content = _png()[:50]
        filename = "truncated-optional.png"
        question = _question("q-corrupt-optional", title="非关键图片损坏的题。",
                             images=[{"path": "assets/" + filename, "required": False,
                                      "field": "title"}])
        out, report = self.run_export([question], media={question["id"]: [filename]},
                                      assets={filename: content})
        word = report["wordExport"]
        self.assertEqual(word["wordQuestionIds"], [question["id"]])
        self.assertEqual(word["manualQuestionIds"], [])
        self.assertEqual(word["formatManualQuestions"], 0)
        self.assertEqual(word["backupManualQuestionIds"], [question["id"]])
        self.assertEqual(word["pending"], [])
        self.assertEqual(word["mediaProblems"], [{
            "questionId": question["id"], "file": "assets/" + filename,
            "reason": "unreadable_media", "required": False,
        }])
        self.assertTrue(word["warnings"], "省略非关键图须在本地报告中告知")
        root, uploaded, _ = _document(report["files"]["questionDocx"])
        self.assertIn(question["title"], uploaded)
        self.assertEqual(root.findall(".//a:blip", NS), [])
        manual = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        self.assertIn(question["title"], manual)
        self.assertIn("assets/" + filename, manual)
        self.assertEqual((out / "assets" / filename).read_bytes(), content)

    def test_readable_required_svg_preserves_attachment_in_format_manual(self):
        content = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                   b'<circle cx="5" cy="5" r="4" fill="blue"/></svg>')
        filename = "readable-vector.svg"
        question = _question("q-vector", title="可读取但Word尚未保证兼容的矢量题图。",
                             images=[{"path": "assets/" + filename, "required": True,
                                      "field": "title"}])
        out, report = self.run_export([question], media={question["id"]: [filename]},
                                      assets={filename: content})
        word = report["wordExport"]
        self.assertEqual(word["manualQuestionIds"], [question["id"]])
        self.assertEqual(word["formatManualQuestions"], 1)
        self.assertEqual(word["pendingQuestions"], 0)
        self.assertEqual(word["pending"], [])
        self.assertEqual(word["mediaProblems"], [{
            "questionId": question["id"], "file": "assets/" + filename,
            "reason": "unsupported_vector_media", "required": True,
        }])
        self.assertEqual(word["formatManual"][0]["reason"], "unsupported_vector_media")
        self.assertFalse(word["formatManual"][0].get("requiresUserApprovalBeforeSupplement"))
        manual = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        self.assertIn(question["title"], manual)
        self.assertIn("assets/" + filename, manual)
        self.assertEqual((out / "assets" / filename).read_bytes(), content)

    def test_successful_pandoc_with_missing_drawing_pauses_only_qa_and_omits_backup(self):
        good_content, qa_content = _png(), _png((30, 80, 200))
        good_filename, qa_filename = "good-drawing.png", "qa-shared-drawing.png"
        questions = [
            _question("q-render-safe", title="其余安全图题继续。",
                      images=[{"path": "assets/" + good_filename, "required": True, "field": "title"}]),
            _question("q-render-qa", "简答", ["不可漏图的完整问答答案。"], title="需要题干和答案题图的问答。",
                      images=[{"path": "assets/" + qa_filename, "required": True, "field": "title"},
                              {"path": "assets/" + qa_filename, "required": True, "field": "answer"}]),
        ]
        original_run = subprocess.run
        omitted = []
        qa_digest = hashlib.sha256(qa_content).hexdigest()

        def successful_pandoc_without_one_drawing(command, *args, **kwargs):
            result = original_run(command, *args, **kwargs)
            if result.returncode:
                return result
            target = Path(command[command.index("--output") + 1])
            with zipfile.ZipFile(target) as package:
                members = [(info, package.read(info.filename)) for info in package.infolist()]
                root = ET.fromstring(package.read("word/document.xml"))
                relationships = ET.fromstring(package.read("word/_rels/document.xml.rels"))
                omitted_rids = {item.get("Id") for item in relationships
                                if item.get("Target", "").startswith("media/")
                                and hashlib.sha256(package.read("word/" + item.get("Target"))).hexdigest() == qa_digest}
            if not omitted_rids:
                return result
            removed = False
            for parent in root.iter():
                for child in list(parent):
                    drawings = child.findall(".//a:blip", NS)
                    if child.tag == "{" + NS["w"] + "}drawing" and any(
                            drawing.get("{" + NS["r"] + "}embed") in omitted_rids for drawing in drawings):
                        parent.remove(child)
                        removed = True
                        break
                if removed:
                    break
            self.assertTrue(removed)
            remaining = [drawing for drawing in root.findall(".//a:blip", NS)
                         if drawing.get("{" + NS["r"] + "}embed") in omitted_rids]
            self.assertEqual(len(remaining), 1, "一次题图还在，必须按引用次数发现另一处遗漏")
            changed_doc = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            rewritten = target.with_suffix(".omitted.docx")
            with zipfile.ZipFile(rewritten, "w") as package:
                for info, content in members:
                    package.writestr(info, changed_doc if info.filename == "word/document.xml" else content)
            rewritten.replace(target)
            omitted.append(target.name)
            return result

        with patch("questionbank.word_export.subprocess.run", side_effect=successful_pandoc_without_one_drawing):
            _, report = self.run_export(questions,
                                        media={"q-render-safe": [good_filename],
                                               "q-render-qa": [qa_filename, qa_filename]},
                                        assets={good_filename: good_content, qa_filename: qa_content})
        self.assertTrue(omitted, "故障须发生在实际成功的Pandoc DOCX中")
        word = report["wordExport"]
        self.assertEqual(word["wordQuestionIds"], ["q-render-safe"])
        self.assertEqual(word["pendingQuestionIds"], ["q-render-qa"])
        self.assertEqual(word["backupManualQuestionIds"], ["q-render-safe"])
        backup = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        self.assertIn("其余安全图题继续", backup)
        self.assertNotIn("需要题干和答案题图的问答", backup)
        self.assertNotIn("不可漏图的完整问答答案", backup)
        self.assertIn("missing_required_image", word["pending"][0]["reasons"])
        self.assertIn({"questionId": "q-render-qa", "file": "assets/" + qa_filename,
                       "reason": "not_embedded_in_docx", "required": True}, word["mediaProblems"])
        root, text, _ = _document(report["files"]["questionDocx"])
        self.assertIn("其余安全图题继续", text)
        self.assertNotIn("需要题干和答案题图的问答", text)
        self.assertNotIn("不可漏图的完整问答答案", text)
        self.assertEqual(len(root.findall(".//a:blip", NS)), 1)
        self.assertNotIn("题图", text, "关闭implicit figures后不应附加图片字幕")

    def test_option_image_stays_with_its_option(self):
        content = _png((0, 128, 255))
        filename = hashlib.sha256(content).hexdigest() + ".png"
        question = _question("q-option-image", title="按选项图选择单位。",
                             images=[{"path": "assets/" + filename, "required": True,
                                      "field": "option", "optionIndex": 0}])
        _, report = self.run_export([question], media={question["id"]: [filename]},
                                    assets={filename: content})
        root, _, _ = _document(report["files"]["questionDocx"])
        body = root.find("w:body", NS)
        elements = list(body)
        index_a = next(i for i, node in enumerate(elements) if "A. 欧姆" in "".join(node.itertext()))
        index_b = next(i for i, node in enumerate(elements) if "B. 伏特" in "".join(node.itertext()))
        drawing_indices = [i for i, node in enumerate(elements) if node.findall(".//a:blip", NS)]
        self.assertTrue(drawing_indices)
        self.assertTrue(all(index_a <= index < index_b for index in drawing_indices))
        word = report["wordExport"]
        self.assertEqual(word["backupManualQuestionIds"], [question["id"]])
        self.assertEqual(word["manualQuestions"], 0)
        self.assertIn({"questionId": question["id"], "field": "option", "optionIndex": 0,
                       "file": "assets/" + filename, "required": True,
                       "platformDisplayVerified": False}, word["mediaAttachments"])

    def test_include_qa_with_readable_media_has_one_backup_without_duplicate_counts(self):
        content, filename = _png((45, 90, 150)), "qa-readable.png"
        question = _question("q-qa-media-backup", "简答", ["按附图核对结果。"],
                             title="可读媒体问答的显式Word复测。",
                             images=[{"path": "assets/" + filename, "field": "title", "required": True}])
        _, report = self.run_export([question], mode="include", media={question["id"]: [filename]},
                                    assets={filename: content})
        word = report["wordExport"]
        self.assertEqual(word["wordQuestions"], 1)
        self.assertEqual(word["manualQuestions"], 0)
        self.assertEqual(word["backupManualQuestionIds"], [question["id"]])
        plain = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        self.assertEqual(plain.count(question["title"]), 1)
        self.assertIn("assets/" + filename, plain)
        self.assertIn({"questionId": question["id"], "field": "title", "file": "assets/" + filename,
                       "required": True, "platformDisplayVerified": False}, word["mediaAttachments"])
        root, text, _ = _document(report["files"]["questionDocx"])
        self.assertIn(question["title"], text)
        self.assertEqual(len(root.findall(".//a:blip", NS)), 1)

    def test_absent_brief_falls_back_without_inventing_or_omitting_question(self):
        reliable = _question("q-fallback", title="可靠解析回退。",
                             originalExplanation="参考讲义：欧姆是电阻的单位。", explanation="")
        reliable.pop("briefExplanation")
        empty = _question("q-no-analysis", title="没有依据解析的题。",
                          originalExplanation="", explanation="")
        empty.pop("briefExplanation")
        _, report = self.run_export([reliable, empty])
        self.assertEqual(report["wordExport"]["wordQuestions"], 2)
        _, text, _ = _document(report["files"]["questionDocx"])
        self.assertIn("参考讲义：欧姆是电阻的单位。", text)
        self.assertIn("没有依据解析的题。", text)
        self.assertNotIn("暂无解析", text)
        self.assertTrue(report["wordExport"]["warnings"])

    def test_equal_byte_limit_is_allowed_and_one_byte_less_routes_to_manual(self):
        questions = [_question("q-boundary", title="严格文件大小边界。")]
        _, baseline = self.run_export(questions)
        real_docx = Path(baseline["files"]["questionDocx"]).read_bytes()
        exact_size = len(real_docx)

        def fixed_real_document(questions, target, out, pandoc, reference, report):
            # Fixed, valid final DOCX bytes prevent timestamps from changing the
            # ZIP's size by a byte between independent renderer invocations.
            target.write_bytes(real_docx)

        with patch("questionbank.word_export._render_docx", side_effect=fixed_real_document), \
                patch("questionbank.word_export._compact_docx", return_value=None):
            _, exact = self.run_export(questions, max_bytes=exact_size)
            self.assertEqual(exact["wordExport"]["wordQuestionIds"], ["q-boundary"])
            self.assertEqual(Path(exact["files"]["questionDocx"]).stat().st_size, exact_size)
            _, report = self.run_export(questions, max_bytes=exact_size - 1)
        self.assertEqual(report["wordExport"]["manualQuestionIds"], ["q-boundary"])
        self.assertEqual(report["wordExport"]["formatManualQuestions"], 1)
        self.assertEqual(report["wordExport"]["pendingQuestions"], 0)
        self.assertEqual(report["files"]["questionDocxParts"], [])
        manual = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        self.assertIn("严格文件大小边界。", manual)

    def test_oversized_question_routes_to_manual_while_other_questions_still_export(self):
        rng = random.Random(41)
        long_text = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(18000))
        short = _question("q-short", title="可上传短题。")
        _, baseline = self.run_export([short])
        budget = Path(baseline["files"]["questionDocx"]).stat().st_size + 256
        _, report = self.run_export([short, _question("q-huge", title="过大题：" + long_text)],
                                    max_bytes=budget)
        self.assertEqual(report["wordExport"]["wordQuestionIds"], ["q-short"])
        self.assertEqual(report["wordExport"]["manualQuestionIds"], ["q-huge"])
        self.assertEqual(report["wordExport"]["formatManualQuestions"], 1)
        self.assertEqual(report["wordExport"]["pendingQuestions"], 0)
        self.assertLessEqual(Path(report["files"]["questionDocx"]).stat().st_size, budget)
        manual = Path(report["files"]["manualQuestions"]).read_text(encoding="utf-8")
        self.assertIn(long_text, manual)

    def test_final_docx_split_keeps_whole_questions_and_repeats_type_heading(self):
        questions, sizes = [], []
        for index in range(3):
            rng = random.Random(100 + index)
            content = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(9000))
            question = _question("q-split-" + str(index), title=f"分包题 {index}：" + content)
            questions.append(question)
            _, single = self.run_export([question])
            sizes.append(Path(single["files"]["questionDocx"]).stat().st_size)
        budget = max(sizes) + 1024
        _, report = self.run_export(questions, max_bytes=budget)
        parts = report["wordExport"]["parts"]
        self.assertGreater(len(parts), 1)
        self.assertEqual(report["wordExport"]["pendingQuestions"], 0)
        for part in parts:
            self.assertLessEqual(Path(part["file"]).stat().st_size, budget)
            _, text, _ = _document(part["file"])
            self.assertIn("单选题", text)
            expected_ids = {item["questionId"] for item in part["questions"]}
            for question in questions:
                if question["id"] in expected_ids:
                    self.assertIn(question["title"], text)
                    number = next(item["number"] for item in part["questions"]
                                  if item["questionId"] == question["id"])
                    self.assertRegex(text, rf"(?m)^{number}[.、．]\s*")
                else:
                    self.assertNotIn(question["title"], text)

    def test_invalid_mode_and_nonpositive_limit_fail_before_creating_word(self):
        for kwargs in ({"word_qa_mode": "unrecognised"}, {"max_bytes": 0}, {"max_bytes": -1}):
            with self.subTest(kwargs=kwargs):
                folder = self.root / ("invalid-" + str(len(list(self.root.iterdir()))))
                folder.mkdir()
                report = {"files": {}, "warnings": []}
                with self.assertRaises(ValueError):
                    self.export_word(_bank([_question("q-safe")]), folder, str(self.pandoc),
                                     report, {}, set(), **kwargs)
                self.assertEqual(list(folder.iterdir()), [])


class ExportPublicationTests(unittest.TestCase):
    """A failed publish must preserve the previously delivered local artifacts."""

    def setUp(self):
        from questionbank.publication import publish_export
        self.publish_export = publish_export
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="publish-acceptance-", dir=base)
        self.root = Path(self.temp.name)
        self.out = self.root / "正式输出"

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, name, files):
        folder = self.root / name
        folder.mkdir()
        for relative, content in files.items():
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        docx = [str(folder / relative) for relative in files if relative.endswith(".docx")]
        report = {"files": {"questionDocxParts": docx}, "warnings": [],
                  "wordExport": {"parts": [{"file": value} for value in docx]}}
        if docx:
            report["files"]["questionDocx"] = docx[0]
        if "问答题手动录入.txt" in files:
            report["files"]["manualQuestions"] = str(folder / "问答题手动录入.txt")
        if "问答题手动录入.md" in files:
            report["files"]["manualQuestionsMarkdown"] = str(folder / "问答题手动录入.md")
        return folder, report

    def snapshot(self):
        return {path.relative_to(self.out).as_posix(): path.read_bytes()
                for path in self.out.rglob("*") if path.is_file()}

    def seed(self):
        stage, report = self.stage("旧批次", {
            "题库.docx": b"previous-word", "题库-02.docx": b"previous-part-two",
            "题库-03.docx": b"previous-part-three", "问答题手动录入.md": b"previous-source",
            "问答题手动录入.txt": b"previous-plain-manual",
            "assets/old.png": _png(),
        })
        self.publish_export(stage, self.out, report)

    def test_replacement_failure_rolls_back_every_delivered_file_and_report(self):
        self.seed()
        before = self.snapshot()
        stage, report = self.stage("新批次", {
            "题库.docx": b"replacement-word", "题库-02.docx": b"replacement-two",
        })
        original_replace = Path.replace
        attempts = 0

        def fail_second_replacement(source, target):
            nonlocal attempts
            if Path(target).resolve().is_relative_to(self.out.resolve()):
                attempts += 1
                if attempts == 2:
                    raise OSError("模拟第二个生成物替换失败")
            return original_replace(source, target)

        with patch.object(Path, "replace", new=fail_second_replacement):
            with self.assertRaisesRegex(OSError, "替换失败"):
                self.publish_export(stage, self.out, report)
        self.assertGreaterEqual(attempts, 2, "故障应发生在已有文件替换之后")
        self.assertEqual(self.snapshot(), before)

    def test_stale_removal_failure_restores_already_removed_old_parts(self):
        self.seed()
        before = self.snapshot()
        stage, report = self.stage("减包新批次", {"题库.docx": b"new-single-word"})
        original_unlink = Path.unlink
        removed_owned = []

        def fail_after_first_stale(source, *args, **kwargs):
            if source.parent == self.out and source.name == "题库-03.docx":
                raise OSError("模拟旧分包删除失败")
            if source.parent == self.out and source.name == "题库-02.docx":
                removed_owned.append(source.name)
            return original_unlink(source, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_after_first_stale):
            with self.assertRaisesRegex(OSError, "删除失败"):
                self.publish_export(stage, self.out, report)
        self.assertEqual(removed_owned, ["题库-02.docx"])
        self.assertEqual(self.snapshot(), before)

    def test_unregistered_different_file_is_preserved_and_publish_is_rejected(self):
        self.seed()
        (self.out / "用户笔记.md").write_text("用户原有笔记", encoding="utf-8")
        before = self.snapshot()
        stage, report = self.stage("冲突新批次", {
            "题库.docx": b"new-word", "用户笔记.md": b"different-generated-content",
        })
        with self.assertRaisesRegex(ValueError, "未登记"):
            self.publish_export(stage, self.out, report)
        self.assertEqual(self.snapshot(), before)

    def test_externally_edited_owned_file_is_preserved_and_publish_is_rejected(self):
        self.seed()
        (self.out / "题库.docx").write_bytes(b"user-modified-the-word")
        before = self.snapshot()
        stage, report = self.stage("外改冲突批次", {"题库.docx": b"new-word"})
        with self.assertRaisesRegex(ValueError, "外部修改"):
            self.publish_export(stage, self.out, report)
        self.assertEqual(self.snapshot(), before)

    def test_smaller_success_cleans_owned_parts_and_rebases_all_report_paths(self):
        self.seed()
        (self.out / "用户笔记.md").write_text("用户原有笔记", encoding="utf-8")
        stage, report = self.stage("成功减包批次", {"题库.docx": b"new-single-word"})
        final = self.publish_export(stage, self.out, report)
        self.assertEqual((self.out / "题库.docx").read_bytes(), b"new-single-word")
        for filename in ["题库-02.docx", "题库-03.docx", "问答题手动录入.md",
                         "问答题手动录入.txt", "assets/old.png"]:
            self.assertFalse((self.out / filename).exists(), filename)
        self.assertEqual((self.out / "用户笔记.md").read_text(encoding="utf-8"), "用户原有笔记")
        expected = str(self.out / "题库.docx")
        self.assertEqual(final["files"]["questionDocxParts"], [expected])
        self.assertEqual(final["files"]["questionDocx"], expected)
        self.assertEqual(final["wordExport"]["parts"][0]["file"], expected)
        saved = json.loads((self.out / "export-report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, final)
        self.assertNotIn(str(stage), json.dumps(saved, ensure_ascii=False))
        manifest = json.loads((self.out / ".export-manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            self.assertEqual(entry["sha256"], hashlib.sha256((self.out / entry["path"]).read_bytes()).hexdigest())
        self.assertNotIn("用户笔记.md", [entry["path"] for entry in manifest["files"]])


if __name__ == "__main__":
    unittest.main()
