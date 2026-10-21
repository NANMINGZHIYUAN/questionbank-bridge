from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib
from urllib.parse import unquote, urlsplit

from questionbank.exporters import export_bundle
from questionbank.core import normalize


def _png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b"")


def _question(qid, kind="单选", answer=None, **fields):
    question = {"id": qid, "type": kind, "title": "电阻的单位是什么？", "options": [{"label": "A", "text": "欧姆"}, {"label": "B", "text": "伏特"}], "correctAnswer": ["A"] if answer is None else answer, "selectedAnswer": ["个人秘密答案"], "rawSelectedAnswer": ["个人秘密原值"], "originalExplanation": "由欧姆定律判断。", "explanation": "", "topicIds": [], "images": [], "sources": [{"sourceId": "src-1", "locator": "questions[0]"}], "issues": []}
    question.update(fields)
    return question


def _bank(media_path):
    good = _question("q-good", title=r"欧姆定律为 \(U=IR\)。哪项正确？", options=[{"label": "A", "text": '欧姆 <img src=x onerror="alert(1)"> & <script>alert(2)</script>'}, {"label": "B", "text": "伏特"}], images=[{"path": str(media_path), "required": True}], topicIds=["t-ohm"])
    multi = _question("q-multi", "多选", ["A", "B"], title="哪些是电学单位？", topicIds=["t-ohm"])
    questions = [good, multi, _question("q-missing", answer=[]), _question("q-conflict", answer=[], issues=[{"code": "conflicting_correct_answers", "message": "来源答案冲突"}], variants=[{"correctAnswer": ["A"], "sources": []}, {"correctAnswer": ["B"], "sources": []}]), _question("q-unknown", "未知"), _question("q-remote", images=[{"url": "https://example.invalid/private.png", "required": True}]), _question("q-invalid", answer=["Z"])]
    topic = {"id": "t-ohm", "title": "欧姆定律", "summary": "电压、电流与电阻的关系。", "explanation": "已提供的知识讲解正文，三者满足 $U=IR$。\n\n\\[R=\\frac{U}{I}\\]\n\n数学表达式 $F(x)=[x](y)$。代码例子 `<script>alert(0)</script>`。", "questionIds": ["q-good", "q-multi", "q-conflict"], "cards": [{"id": "c-ohm", "prompt": "欧姆定律的关系式是什么？", "answer": r"\(U=IR\)", "questionIds": ["q-good"]}, {"id": "c-units", "prompt": "两个电学单位是什么？", "answer": "欧姆和伏特。", "questionIds": ["q-multi"]}, {"id": "c-conflict", "prompt": "冲突题的正确选项是什么？", "answer": "A", "questionIds": ["q-conflict"]}]}
    return {"schemaVersion": 1, "course": "电路基础", "sources": [{"id": "src-1", "path": str(media_path.parent / "source.json"), "sha256": "f" * 64, "format": "good-review", "url": "https://mooc1.chaoxing.com/exam/private?courseid=secret"}], "questions": questions, "topics": [topic]}


class ExportBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import genanki  # noqa: F401
            import docx  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"项目导出依赖未就绪: {exc}")
        cls.pandoc = shutil.which("pandoc")
        if not cls.pandoc:
            raise unittest.SkipTest("Pandoc 未安装")
        temp_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="exporters-", dir=temp_root)
        cls.root = Path(cls.temp.name)
        cls.image = cls.root / "图.png"
        cls.image.write_bytes(_png())
        cls.bank = _bank(cls.image)
        cls.original = copy.deepcopy(cls.bank)
        cls.out = cls.root / "导出 含空格"
        cls.report = export_bundle(cls.bank, cls.out, str(cls.pandoc))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def _collection(self, path, destination):
        with zipfile.ZipFile(path) as package:
            media = json.loads(package.read("media"))
            database = destination / "collection.anki2"
            database.write_bytes(package.read("collection.anki2"))
            media_bytes = {name: package.read(number) for number, name in media.items()}
        with sqlite3.connect(database) as connection:
            notes = connection.execute("SELECT guid, mid, flds FROM notes ORDER BY guid").fetchall()
            decks = json.loads(connection.execute("SELECT decks FROM col").fetchone()[0])
            models = json.loads(connection.execute("SELECT models FROM col").fetchone()[0])
        return notes, decks, models, media_bytes

    def test_bank_preserved_and_all_expected_artifacts_exist(self):
        self.assertEqual(self.bank, self.original)
        self.assertNotIn("knowledgeDocx", self.report["files"])
        self.assertFalse((self.out / "知识讲解.docx").exists())
        json.dumps(self.report, ensure_ascii=False)
        for key in ["normalizedJson", "questionMarkdown", "knowledgeMarkdown", "questionDocx", "anki", "knowledgeMap", "questionIndex", "sourceIndex"]:
            self.assertTrue(Path(self.report["files"][key]).is_file(), key)
        saved = json.loads((self.out / "电路基础题库.json").read_text(encoding="utf-8"))
        self.assertNotIn("selectedAnswer", saved["questions"][0])
        self.assertNotIn("rawSelectedAnswer", saved["questions"][0])
        self.assertNotIn("个人秘密", json.dumps(saved, ensure_ascii=False))
        self.assertEqual(saved["questions"][5]["images"][0]["url"], "https://example.invalid/private.png")
        self.assertTrue((self.out / saved["questions"][0]["images"][0]["path"]).is_file())

    def test_case_and_canonical_answer_text_remain_complete_across_outputs(self):
        bank = copy.deepcopy(self.bank)
        bank['topics'] = []
        bank['questions'] = [_question('q-case', answer=['B'], title='按照案例选择。',
            case='案例有必要条件，必须随题保留。\n我的答案：PRIVATE_CASE\n1. PRIVATE_STEP',
            options=[{'label':'A','text':'B'}, {'label':'B','text':'C'}],
            originalExplanation='可靠原解析。\n我的答案：PRIVATE_EXPLANATION\nPRIVATE_CONTINUATION',
            explanation='可靠补充。', briefExplanation='可靠简析。')]
        original = copy.deepcopy(bank)
        out = self.root / '案例与正文清理'
        report = export_bundle(bank, out, str(self.pandoc))
        saved = Path(report['files']['normalizedJson']).read_text(encoding='utf-8')
        markdown = Path(report['files']['questionMarkdown']).read_text(encoding='utf-8')
        notes, _, _, _ = self._collection(report['files']['anki'], out)
        fields = '\n'.join(note[2] for note in notes)
        with zipfile.ZipFile(report['files']['questionDocx']) as document:
            word = ''.join(ET.fromstring(document.read('word/document.xml')).itertext())
        for text in (saved, markdown, fields, word):
            self.assertIn('案例有必要条件，必须随题保留。', text)
            self.assertNotIn('PRIVATE_', text)
        self.assertIn('可靠原解析。', saved)
        self.assertIn('A. B', markdown)
        self.assertIn('A. B', fields)
        self.assertIn('答案：A', word)
        self.assertEqual(report['wordExport']['wordQuestions'], 1)
        self.assertEqual(bank, original)

    def test_question_and_topic_cards_skip_uncertain_answers(self):
        self.assertEqual(self.report["originalCards"], 2)
        self.assertEqual(self.report["topicCards"], 2)
        self.assertEqual(self.report["skippedOriginalCards"], 5)
        self.assertEqual(self.report["skippedTopicCards"], 1)
        document = (self.out / "电路基础题库.md").read_text(encoding="utf-8")
        self.assertIn("q-conflict", document)
        self.assertIn("存在答案冲突", document)
        self.assertIn("q-remote", document)
        self.assertIn("missing_required_image", document)
        self.assertNotIn("https://example.invalid", document)
        self.assertEqual(len(self.report["missingMedia"]), 1)

    def test_safe_anki_fields_local_media_and_two_decks(self):
        notes, decks, _, media = self._collection(self.report["files"]["anki"], self.root)
        self.assertEqual(len(notes), 4)
        fields = "\n".join(note[2] for note in notes)
        self.assertNotIn("<script", fields)
        self.assertNotIn("<img src=x", fields)
        self.assertNotIn("onerror=", fields)
        self.assertNotIn("个人秘密", fields)
        self.assertNotIn("https://example.invalid", fields)
        self.assertNotIn("chaoxing.com", fields)
        self.assertIn("&amp;", fields)
        self.assertIn(r"\(U=IR\)", fields)
        deck_names = {item["name"] for item in decks.values()}
        self.assertIn("电路基础::原题卡", deck_names)
        self.assertIn("电路基础::考点卡", deck_names)
        self.assertEqual(len(media), 1)
        self.assertEqual(next(iter(media.values())), _png())
        media_name = next(iter(media))
        self.assertRegex(media_name, r"^[a-f0-9]{64}\.png$")
        self.assertIn(media_name, fields)

    def test_regeneration_retains_ids_and_guids(self):
        second = self.root / "再生成"
        changed = copy.deepcopy(self.bank)
        changed["questions"][0]["explanation"] = "内容更新，不改变原题身份。"
        report = export_bundle(changed, second, str(self.pandoc))
        notes1, decks1, models1, _ = self._collection(self.report["files"]["anki"], self.root)
        notes2, decks2, models2, _ = self._collection(report["files"]["anki"], second)
        self.assertEqual([note[0] for note in notes1], [note[0] for note in notes2])
        self.assertEqual(set(decks1), set(decks2))
        self.assertEqual(set(models1), set(models2))
        self.assertNotEqual([note[2] for note in notes1], [note[2] for note in notes2])

    def test_knowledge_map_details_and_links_match_existing_pages(self):
        map_path = Path(self.report["files"]["knowledgeMap"])
        contents = map_path.read_text(encoding="utf-8")
        self.assertIn("电压、电流与电阻的关系", contents)
        self.assertNotIn("已提供的知识讲解正文", contents)
        topic_path = Path(self.report["files"]["topicPages"][0])
        topic = topic_path.read_text(encoding="utf-8")
        self.assertTrue(topic.startswith("# 01 欧姆定律\n\n[返回课程知识地图]"))
        self.assertTrue(topic_path.name.startswith("01-"))
        self.assertIn("已提供的知识讲解正文", topic)
        self.assertIn("q-good", topic)
        self.assertIn("$F(x)=[x](y)$", topic)
        self.assertIn("`<script>alert(0)</script>`", topic)
        self.assertIn("未展示为确定答案问答", topic)
        for path in (self.out / "knowledge").glob("*.md"):
            parsed = subprocess.run([str(self.pandoc), str(path), "--from", "markdown+tex_math_dollars+tex_math_single_backslash-raw_html", "--to", "json"], capture_output=True, text=True, encoding="utf-8", check=True)
            def links(node):
                if isinstance(node, dict):
                    if node.get("t") == "Link":
                        yield node["c"][2][0]
                    for value in node.values():
                        yield from links(value)
                elif isinstance(node, list):
                    for value in node:
                        yield from links(value)
            for target in links(json.loads(parsed.stdout)):
                if urlsplit(target).scheme in {"http", "https"}:
                    continue
                filename = unquote(target.split("#", 1)[0])
                self.assertTrue((path.parent / filename).is_file(), (path.name, target))

    def test_word_has_real_body_math_images_and_readable_chinese_styles(self):
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main", "m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}
        for filename in ["电路基础题库.docx"]:
            with zipfile.ZipFile(self.out / filename) as document:
                root = ET.fromstring(document.read("word/document.xml"))
                styles = ET.fromstring(document.read("word/styles.xml"))
                text = "".join(root.itertext())
                self.assertGreater(len(root.findall(".//m:oMath", ns)), 0)
                self.assertNotIn(r"\(U=IR\)", text)
                self.assertNotIn(r"\frac", text)
                self.assertIn("单选题", text)
                normal = styles.find("w:style[@w:styleId='Normal']", ns)
                self.assertEqual(normal.find("w:rPr/w:sz", ns).get("{" + ns["w"] + "}val"), "22")
                self.assertEqual(normal.find("w:rPr/w:rFonts", ns).get("{" + ns["w"] + "}eastAsia"), "Microsoft YaHei")
                heading = styles.find("w:style[@w:styleId='Heading1']", ns)
                self.assertEqual(heading.find("w:rPr/w:color", ns).get("{" + ns["w"] + "}val"), "000000")
                if filename == "电路基础题库.docx":
                    self.assertIn("答案：", text)
                    self.assertIn("解析：", text)
                    self.assertTrue(any(path.startswith("word/media/") for path in document.namelist()))

    def test_no_topics_does_not_invent_knowledge(self):
        bank = copy.deepcopy(self.bank)
        bank["topics"] = []
        out = self.root / "无主题"
        report = export_bundle(bank, out, str(self.pandoc))
        self.assertEqual(report["topicCards"], 0)
        self.assertEqual(report["files"]["topicPages"], [])
        text = (out / "电路基础知识讲解.md").read_text(encoding="utf-8")
        self.assertIn("未提供课程主题和知识讲解", text)
        self.assertNotIn("已提供的知识讲解正文", text)
        self.assertNotIn("## 01", text)

    def test_real_normalize_option_text_answers_export_without_skipping(self):
        raw = {"items": [_question("original-1"), _question("original-2", "多选", ["A", "B"], title="电学单位有哪些？")]}
        for item in raw["items"]:
            item["sources"] = []
        bank = normalize(raw, "真实规范数据", {"path": str(self.root / "input.json"), "sha256": "a" * 64, "format": "goodreview"})
        self.assertEqual(bank["questions"][0]["correctAnswer"], ["欧姆"])
        self.assertEqual(bank["questions"][1]["correctAnswer"], ["欧姆", "伏特"])
        report = export_bundle(bank, self.root / "真实规范数据导出", str(self.pandoc))
        self.assertEqual(report["originalCards"], 2)
        self.assertEqual(report["skippedOriginalCards"], 0)
        text = Path(report["files"]["questionMarkdown"]).read_text(encoding="utf-8")
        self.assertIn("A. 欧姆", text)
        self.assertIn("B. 伏特", text)

    def test_direct_api_rejects_malformed_or_source_overwriting_output(self):
        out = self.root / "不应创建"
        with self.assertRaises(ValueError):
            export_bundle({"schemaVersion": 1}, out, str(self.pandoc))
        self.assertFalse(out.exists())
        bank = copy.deepcopy(self.bank)
        bank["sources"][0]["path"] = str(self.out / "原始输入.json")
        with self.assertRaises(ValueError):
            export_bundle(bank, self.out, str(self.pandoc))

    def test_dynamic_question_diagnostics_are_retained_and_skip_cards(self):
        bank = copy.deepcopy(self.bank)
        bank["questions"] = [_question("q-double", answer=["A", "B"])]
        bank["topics"] = []
        report = export_bundle(bank, self.root / "诊断", str(self.pandoc))
        self.assertEqual(report["originalCards"], 0)
        saved = json.loads(Path(report["files"]["normalizedJson"]).read_text(encoding="utf-8"))
        codes = {issue["code"] for issue in saved["questions"][0]["issues"]}
        self.assertIn("invalid_answer_cardinality", codes)

    def test_safe_citations_clickable_in_markdown_word_and_anki(self):
        bank = copy.deepcopy(self.bank)
        citation = "[NREL 官方资料](https://www.nrel.gov/grid/solar-resource-maps.html?a=1&b=2)"
        explanation = "可靠引用：" + citation + "。公式 $F(x)=[x](y)$。代码 `[示例](file:///literal)`。"
        bank["questions"][0]["explanation"] = explanation
        bank["questions"][0]["briefExplanation"] = citation
        bank["topics"][0]["explanation"] += "\n\n" + explanation
        bank["topics"][0]["cards"][0]["answer"] += "。" + citation
        out = self.root / "引用导出"
        report = export_bundle(bank, out, str(self.pandoc))
        self.assertIn(citation, (out / "电路基础题库.md").read_text(encoding="utf-8"))
        self.assertIn(citation, (out / "电路基础知识讲解.md").read_text(encoding="utf-8"))
        self.assertIn("$F(x)=[x](y)$", (out / "电路基础知识讲解.md").read_text(encoding="utf-8"))
        self.assertIn("`[示例](file:///literal)`", (out / "电路基础知识讲解.md").read_text(encoding="utf-8"))
        notes, _, _, _ = self._collection(report["files"]["anki"], out)
        fields = "\n".join(note[2] for note in notes)
        self.assertIn('<a href="https://www.nrel.gov/grid/solar-resource-maps.html?a=1&amp;b=2"', fields)
        self.assertIn(r"\(F(x)=[x](y)\)", fields)
        for name in ("电路基础题库.docx",):
            with zipfile.ZipFile(out / name) as document:
                relationships = ET.fromstring(document.read("word/_rels/document.xml.rels"))
                self.assertTrue(any(item.get("Target") == "https://www.nrel.gov/grid/solar-resource-maps.html?a=1&b=2" for item in relationships))

    def test_dangerous_private_citations_removed_and_source_index_redacted(self):
        bank = copy.deepcopy(self.bank)
        urls = ["javascript:alert(1)", "file:///D:/private/question.docx", "D:/private/question.docx", "../private/question.docx", "https://user:password@www.nrel.gov/private", "https://www.nrel.gov%40evil.example/path", "https://mooc1.chaoxing.com/exam/private?courseid=secret", "https://www.xueyinonline.com/private"]
        bank["questions"][0]["explanation"] = "\n".join(f"[引用{i}]({url})" for i, url in enumerate(urls))
        bank["questions"][0]["sources"][0]["locator"] = "pages=2,3;lines=10-20;id=q-2;D:/private/question.docx;https://mooc1.chaoxing.com/exam/private"
        out = self.root / "安全引用导出"
        report = export_bundle(bank, out, str(self.pandoc))
        markdown = "\n".join(path.read_text(encoding="utf-8") for path in out.rglob("*.md"))
        notes, _, _, _ = self._collection(report["files"]["anki"], out)
        fields = "\n".join(note[2] for note in notes)
        for value in (markdown, fields):
            self.assertNotIn("password@", value)
            self.assertNotIn("chaoxing.com", value)
            self.assertNotIn("xueyinonline.com", value)
            self.assertNotIn("D:/private", value)
            self.assertNotIn("evil.example", value)
            self.assertNotIn("javascript:", value)
        index = Path(report["files"]["questionIndex"]).read_text(encoding="utf-8")
        self.assertIn("pages=2,3；lines=10-20；id=q-2", index)
        source_index = Path(report["files"]["sourceIndex"]).read_text(encoding="utf-8")
        self.assertIn("| src-1 | good-review | 7 |", source_index)
        self.assertNotIn(str(self.root), source_index)
        self.assertIn("来源索引", Path(report["files"]["knowledgeMap"]).read_text(encoding="utf-8"))

    def test_legacy_personal_fields_and_raw_text_are_removed_recursively(self):
        bank = copy.deepcopy(self.bank)
        bank["questions"][0]["rawText"] = "1. 欧姆定律是什么？\n我的答案：PERSONAL-SECRET\n正确答案：U=IR\n解析：电压、电流、电阻的关系。"
        bank["questions"][3]["variants"][0]["selectedAnswer"] = ["PERSONAL-VARIANT"]
        bank["questions"][3]["variants"][0]["rawSelectedAnswer"] = ["PERSONAL-RAW-VARIANT"]
        out = self.root / "个人字段清理"
        report = export_bundle(bank, out, str(self.pandoc))
        saved = Path(report["files"]["normalizedJson"]).read_text(encoding="utf-8")
        for value in ("selectedAnswer", "rawSelectedAnswer", "PERSONAL-SECRET", "PERSONAL-VARIANT", "PERSONAL-RAW-VARIANT"):
            self.assertNotIn(value, saved)
        self.assertIn("正确答案：U=IR", saved)
        self.assertIn("原始答案与来源原值保留", (out / "电路基础题库.md").read_text(encoding="utf-8"))
        self.assertIn("selectedAnswer", bank["questions"][0])


if __name__ == "__main__":
    unittest.main()
