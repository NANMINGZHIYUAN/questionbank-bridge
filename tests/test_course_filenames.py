"""Course-labelled delivery names, checked against real bundle publication."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from questionbank import exporters


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _png():
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


class CourseFilenameTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="course-names-", dir=base)
        self.out = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_file(self, name):
        path = self.out / name
        path.write_bytes(("原文和公式 $U=IR$：" + name).encode("utf-8"))
        return str(path)

    def test_all_registered_deliveries_rebase_nested_report_without_changing_bytes(self):
        names = {
            "normalizedJson": "统一题库.json", "questionMarkdown": "题库.md",
            "knowledgeMarkdown": "知识讲解.md", "manualQuestions": "问答题手动录入.txt",
            "manualQuestionsMarkdown": "问答题手动录入.md", "fojiaoQaText": "佛脚问答批量导入.txt",
        }
        files = {key: self.make_file(name) for key, name in names.items()}
        parts = [self.make_file("题库.docx"), self.make_file("题库-02.docx"),
                 self.make_file("题库-03.docx")]
        anki = self.make_file("演示课程-通识类.apkg")
        files.update(questionDocx=parts[0], questionDocxParts=parts, anki=anki)
        report = {"files": files, "wordExport": {"parts": [{"file": path} for path in parts]},
                  "fojiaoQaExport": {"parts": [{"file": files["fojiaoQaText"]}]},
                  "exactOnly": files["questionMarkdown"] + "#Q001"}
        # Record the independent file contents, not an expected implementation map.
        old_hashes = {key: _sha(Path(files[key])) for key in names}
        part_hashes = [_sha(Path(path)) for path in parts]
        anki_hash = _sha(Path(anki))
        exporters._course_filenames(self.out, report, "演示课程-通识类")
        expected = {
            "normalizedJson": "演示课程-通识类题库.json",
            "questionMarkdown": "演示课程-通识类题库.md",
            "knowledgeMarkdown": "演示课程-通识类知识讲解.md",
            "manualQuestions": "演示课程-通识类问答题手动录入.txt",
            "manualQuestionsMarkdown": "演示课程-通识类问答题手动录入.md",
            "fojiaoQaText": "演示课程-通识类佛脚问答批量导入.txt",
        }
        for key, filename in expected.items():
            target = Path(report["files"][key])
            self.assertEqual(target, self.out / filename)
            self.assertEqual(_sha(target), old_hashes[key])
            self.assertFalse((self.out / names[key]).exists())
        for index, entry in enumerate(report["wordExport"]["parts"]):
            suffix = "" if index == 0 else f"-{index + 1:02d}"
            target = self.out / f"演示课程-通识类题库{suffix}.docx"
            self.assertEqual(Path(entry["file"]), target)
            self.assertEqual(Path(report["files"]["questionDocxParts"][index]), target)
            self.assertEqual(_sha(target), part_hashes[index])
        self.assertEqual(report["files"]["questionDocx"], report["wordExport"]["parts"][0]["file"])
        self.assertEqual(report["fojiaoQaExport"]["parts"][0]["file"], report["files"]["fojiaoQaText"])
        self.assertEqual(report["exactOnly"], str(self.out / "题库.md") + "#Q001")
        self.assertEqual(report["files"]["anki"], anki)
        self.assertEqual(_sha(Path(anki)), anki_hash)
        after = {path.name: _sha(path) for path in self.out.iterdir() if path.is_file()}
        exporters._course_filenames(self.out, report, "演示课程-通识类")
        self.assertEqual({path.name: _sha(path) for path in self.out.iterdir() if path.is_file()}, after)

    def test_course_names_are_safe_and_do_not_create_subdirectories(self):
        cases = [("  实验:室/安全?  ", "实验-室-安全-"),
                 ("CON", "_CON"), ("...", "未命名课程"),
                 ("甲" * 140, "甲" * 100)]
        for course, prefix in cases:
            with self.subTest(course=course):
                report = {"files": {"questionMarkdown": self.make_file("题库.md")}}
                exporters._course_filenames(self.out, report, course)
                target = Path(report["files"]["questionMarkdown"])
                self.assertEqual(target.name, prefix + "题库.md")
                self.assertEqual(target.parent, self.out)

    def test_unregistered_files_and_absent_word_are_untouched(self):
        user_file = Path(self.make_file("题库.docx"))
        before = user_file.read_bytes()
        report = {"files": {"questionMarkdown": self.make_file("题库.md")}}
        exporters._course_filenames(self.out, report, "虚构课程")
        self.assertEqual(user_file.read_bytes(), before)
        self.assertNotIn("questionDocx", report["files"])
        self.assertFalse((self.out / "虚构课程题库.docx").exists())

    def test_existing_user_delivery_name_stops_before_any_staged_rename(self):
        report = {"files": {"normalizedJson": self.make_file("统一题库.json"),
                            "questionMarkdown": self.make_file("题库.md")}}
        self.make_file("虚构课程题库.md")
        before = {path.name: path.read_bytes() for path in self.out.iterdir()}
        with self.assertRaises(ValueError):
            exporters._course_filenames(self.out, report, "虚构课程")
        self.assertEqual({path.name: path.read_bytes() for path in self.out.iterdir()}, before)


class CourseFilenamePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import genanki  # noqa: F401
            import docx  # noqa: F401
        except ImportError as error:
            raise unittest.SkipTest(str(error))
        cls.pandoc = shutil.which("pandoc")
        if not cls.pandoc:
            raise unittest.SkipTest("Pandoc 未安装")

    def test_existing_legacy_manifest_migrates_and_user_notes_survive(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="course-name-publish-", dir=base) as folder:
            out = Path(folder) / "交付"
            questions = [
                {"id": "q-unit", "type": "单选", "title": "虚构课程的电阻单位？",
                 "options": [{"label": "A", "text": "欧姆"}, {"label": "B", "text": "伏特"}],
                 "correctAnswer": ["A"]},
                {"id": "q-qa", "type": "简答", "title": "虚构课程的关系式？",
                 "options": [], "correctAnswer": ["电压等于电流乘以电阻。"]},
            ]
            for question in questions:
                question.update(originalExplanation="虚构资料提供的解析。", explanation="",
                                briefExplanation="由物理量定义判断。", topicIds=[],
                                images=[], sources=[], issues=[])
            bank = {"schemaVersion": 1, "course": "命名虚构课程", "sources": [],
                    "questions": questions, "topics": []}
            with patch.object(exporters, "_course_filenames", side_effect=lambda _out, report, _course: report):
                old = exporters.export_bundle(bank, out, self.pandoc)
            old_names = {Path(path).name for key, path in old["files"].items()
                         if key in {"normalizedJson", "questionMarkdown", "knowledgeMarkdown",
                                    "questionDocx", "manualQuestions", "manualQuestionsMarkdown", "fojiaoQaText"}}
            self.assertIn("题库.docx", old_names)
            self.assertIn("佛脚问答批量导入.txt", old_names)
            user = out / "用户笔记.md"
            user.write_text("用户手写笔记应当保留。", encoding="utf-8")
            final = exporters.export_bundle(bank, out, self.pandoc)
            for key in ("normalizedJson", "questionMarkdown", "knowledgeMarkdown", "questionDocx",
                        "manualQuestions", "manualQuestionsMarkdown", "fojiaoQaText", "anki"):
                target = Path(final["files"][key])
                self.assertTrue(target.is_file(), key)
                self.assertTrue(target.name.startswith("命名虚构课程"), target.name)
            for name in old_names:
                self.assertFalse((out / name).exists(), name)
            self.assertEqual(user.read_text(encoding="utf-8"), "用户手写笔记应当保留。")
            self.assertEqual(final["wordExport"]["parts"][0]["file"], final["files"]["questionDocx"])
            self.assertEqual(final["fojiaoQaExport"]["parts"][0]["file"], final["files"]["fojiaoQaText"])
            manifest = json.loads((out / ".export-manifest.json").read_text(encoding="utf-8"))
            owned = {entry["path"] for entry in manifest["files"]}
            self.assertTrue(old_names.isdisjoint(owned))
            self.assertNotIn("用户笔记.md", owned)
            for entry in manifest["files"]:
                self.assertEqual(_sha(out / entry["path"]), entry["sha256"])
            saved = json.loads((out / "export-report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, final)
            normalized = json.loads(Path(final["files"]["normalizedJson"]).read_text(encoding="utf-8"))
            self.assertEqual([question["id"] for question in normalized["questions"]], ["q-unit", "q-qa"])

    def test_named_legacy_report_without_manifest_cleans_only_registered_media(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="course-name-legacy-", dir=base) as folder:
            root = Path(folder)
            out = root / "交付"
            image = root / "虚构题图.png"
            image.write_bytes(_png())
            question = {"id": "q-image", "type": "单选", "title": "虚构图片的颜色？",
                        "options": [{"label": "A", "text": "白色"}, {"label": "B", "text": "黑色"}],
                        "correctAnswer": ["A"], "originalExplanation": "虚构资料原解析。",
                        "explanation": "", "briefExplanation": "按图判断。", "topicIds": [],
                        "images": [{"path": str(image), "required": True}], "sources": [], "issues": []}
            bank = {"schemaVersion": 1, "course": "旧清单虚构课程", "sources": [],
                    "questions": [question], "topics": []}
            first = exporters.export_bundle(bank, out, self.pandoc)
            saved = json.loads(Path(first["files"]["normalizedJson"]).read_text(encoding="utf-8"))
            old_asset = out / saved["questions"][0]["images"][0]["path"]
            self.assertTrue(old_asset.is_file())
            (out / ".export-manifest.json").unlink()
            user = out / "assets" / "用户资料.txt"
            user.write_text("不得移除", encoding="utf-8")
            question["images"] = []
            final = exporters.export_bundle(bank, out, self.pandoc)
            self.assertTrue(Path(final["files"]["normalizedJson"]).is_file())
            self.assertFalse(old_asset.exists())
            self.assertEqual(user.read_text(encoding="utf-8"), "不得移除")
            manifest = json.loads((out / ".export-manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("assets/用户资料.txt", {entry["path"] for entry in manifest["files"]})


if __name__ == "__main__":
    unittest.main()
