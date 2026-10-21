"""End-to-end local CLI checks with fictional sources and actual exporters."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

from questionbank.cli import main, read_bank, write_json
from questionbank.core import file_sha256


class WordCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import docx  # noqa: F401
            import genanki  # noqa: F401
        except ImportError as error:
            raise unittest.SkipTest(f"完整导出依赖未就绪：{error}")
        if not shutil.which("pandoc"):
            raise unittest.SkipTest("Pandoc 未安装")

    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="word-cli-", dir=base)
        self.root = Path(self.temp.name)
        self.source = self.root / "原件 含空格" / "source.json"
        self.bank_path = self.root / "工作数据" / "bank.json"
        self.out = self.root / "最终输出 含空格"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        # Fictional format fixtures must never publish to the user's vault.
        if args and args[0] == "export":
            args = (*args, "--skip-knowledge-publication")
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(arg) for arg in args])
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_cli_ok(self, *args):
        code, stdout, stderr = self.run_cli(*args)
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def ingest(self, items=None):
        if items is None:
            items = [
                {"type": "单选", "title": "电阻的单位是什么？",
                 "options": [{"label": "A", "text": "欧姆"}, {"label": "B", "text": "伏特"}],
                 "correctAnswer": ["A"]},
                {"type": "多选", "title": "哪些是单位？",
                 "options": [{"label": "A", "text": "欧姆"}, {"label": "B", "text": "伏特"}],
                 "correctAnswer": ["AB"]},
                {"type": "判断", "title": "欧姆是电阻单位。", "correctAnswer": ["true"]},
                {"type": "填空", "title": "填写数值。", "correctAnswer": ["1.5"]},
                {"type": "简答", "title": "解释电压。", "correctAnswer": ["单位电荷通过两点的电势能差。"]},
                {"type": "单选", "title": "电压的单位是什么？",
                 "options": [{"label": "A", "text": "伏特"}, {"label": "B", "text": "欧姆"}],
                 "correctAnswer": ["A"]},
            ]
        for item in items:
            item.setdefault("briefExplanation", "先明确物理量，再选择对应单位或关系。")
            item.setdefault("originalExplanation", "虚构试题的已提供解析。")
            item.setdefault("selectedAnswer", ["PERSONAL-FICTIONAL-SECRET"])
        write_json(self.source, {"items": items})
        original = self.source.read_bytes()
        result = self.assert_cli_ok("ingest", self.source, "--course", "CLI虚构课程",
                                    "--out", self.bank_path.parent)
        self.assertEqual(Path(result["bank"]), self.bank_path)
        self.assertEqual(self.source.read_bytes(), original)
        return read_bank(self.bank_path)

    def snapshot(self):
        return {path.relative_to(self.out).as_posix(): path.read_bytes()
                for path in self.out.rglob("*") if path.is_file()}

    def assert_final_report(self, result):
        saved = json.loads((self.out / "export-report.json").read_text(encoding="utf-8"))
        file_report = {key: value for key, value in result.items() if key != "knowledgePublication"}
        self.assertEqual(saved, file_report)
        self.assertTrue(result["sourceHashesUnchanged"])
        self.assertFalse(result["actualFojiaoImportVerified"])
        self.assertFalse(result["wordExport"]["platformImportVerified"])
        word = result["wordExport"]
        self.assertEqual(word["selectedQuestions"], word["wordQuestions"]
                         + word["manualQuestions"] + word["pendingQuestions"])
        for path in result["files"]["questionDocxParts"]:
            target = Path(path)
            self.assertTrue(target.is_file())
            self.assertTrue(target.is_relative_to(self.out))
            self.assertLessEqual(target.stat().st_size, 500000)
        if result["files"].get("manualQuestions"):
            for key, suffix in [("manualQuestions", ".txt"), ("manualQuestionsMarkdown", ".md")]:
                target = Path(result["files"][key])
                self.assertEqual(target.suffix, suffix)
                self.assertTrue(target.is_file())
                self.assertTrue(target.is_relative_to(self.out))
        qa = result["fojiaoQaExport"]
        self.assertEqual(qa["selectedQuestions"], qa["exportedQuestions"] + qa["deferredQuestions"])
        self.assertCountEqual(qa["exportedQuestionIds"] + qa["deferredQuestionIds"], qa["selectedQuestionIds"])
        self.assertEqual(len(qa["selectedQuestionIds"]), len(set(qa["selectedQuestionIds"])))
        self.assertFalse(qa["platformImportVerified"])
        self.assertFalse(qa["platformTextLimitVerified"])
        if qa["exportedQuestions"]:
            target = Path(result["files"]["fojiaoQaText"])
            self.assertTrue(target.is_relative_to(self.out))
            raw = target.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            rows = json.loads(raw.decode("utf-8"))
            self.assertEqual(len(rows), qa["exportedQuestions"])
            self.assertEqual(len(qa["parts"]), 1)
            self.assertEqual(qa["parts"][0]["file"], str(target))
            self.assertEqual(qa["parts"][0]["bytes"], len(raw))
            self.assertEqual(qa["parts"][0]["characters"], len(raw.decode("utf-8")))
            self.assertEqual([item["questionId"] for item in qa["parts"][0]["questions"]],
                             qa["exportedQuestionIds"])
            for row in rows:
                self.assertEqual(set(row), {"题型", "题干", "答案", "解析"})
                self.assertEqual(row["题型"], "问答题")
        else:
            self.assertNotIn("fojiaoQaText", result["files"])
            self.assertFalse(qa["parts"])
        self.assertNotIn("stage-", json.dumps(result["files"], ensure_ascii=False))

    def test_sample_include_and_export_manual_use_real_cli_and_final_paths(self):
        bank = self.ingest()
        original_source = self.source.read_bytes()
        original_bank = self.bank_path.read_bytes()
        sample = self.assert_cli_ok("sample", self.bank_path, "--out", self.out,
                                    "--limit", "5", "--word-qa-mode", "include")
        self.assert_final_report(sample)
        self.assertTrue(sample["sample"])
        self.assertEqual(sample["wordExport"]["selectedQuestions"], 5)
        self.assertEqual(sample["wordExport"]["wordQuestions"], 5)
        self.assertEqual(sample["wordExport"]["manualQuestions"], 0)
        self.assertEqual(len(sample["wordExport"]["backupManualQuestionIds"]), 1)
        sample_qa = json.loads(Path(sample["files"]["fojiaoQaText"]).read_text(encoding="utf-8"))
        self.assertEqual(sample["fojiaoQaExport"]["exportedQuestions"], 1)
        exported = self.assert_cli_ok("export", self.bank_path, "--out", self.out,
                                      "--word-qa-mode", "manual")
        self.assert_final_report(exported)
        self.assertFalse(exported["sample"])
        self.assertEqual(exported["wordExport"]["selectedQuestions"], len(bank["questions"]))
        self.assertEqual(exported["wordExport"]["manualQuestions"], 1)
        self.assertEqual(exported["wordExport"]["backupManualQuestionIds"], [])
        self.assertTrue(Path(exported["files"]["manualQuestions"]).is_file())
        exported_qa = json.loads(Path(exported["files"]["fojiaoQaText"]).read_text(encoding="utf-8"))
        self.assertEqual(exported_qa, sample_qa, "Word include/manual 均须额外导出同一问答 JSON")
        self.assertEqual(self.source.read_bytes(), original_source)
        self.assertEqual(self.bank_path.read_bytes(), original_bank)

    def test_default_cli_routes_qa_to_copyable_plain_text(self):
        bank = self.ingest()
        result = self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        self.assert_final_report(result)
        self.assertEqual(result["wordExport"]["qaMode"], "manual")
        self.assertEqual(result["wordExport"]["manualQuestions"], 1)
        self.assertEqual(result["wordExport"]["wordQuestions"], len(bank["questions"]) - 1)
        plain = Path(result["files"]["manualQuestions"]).read_text(encoding="utf-8")
        for value in ["【题干】", "【答案】", "【解析】", "解释电压。", "单位电荷通过两点的电势能差。"]:
            self.assertIn(value, plain)
        self.assertNotIn("### ", plain)
        qa_rows = json.loads(Path(result["files"]["fojiaoQaText"]).read_text(encoding="utf-8"))
        self.assertEqual(qa_rows[0]["题干"], "解释电压。")
        self.assertEqual(qa_rows[0]["答案"], "单位电荷通过两点的电势能差。")

    def test_real_size_split_then_lean_export_removes_only_old_parts_and_manual(self):
        items = []
        for index in range(4):
            rng = random.Random(830 + index)
            text = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(280000))
            items.append({"type": "单选", "title": f"真实尺寸分包题{index}：" + text,
                          "options": [{"label": "A", "text": "欧姆"}, {"label": "B", "text": "伏特"}],
                          "correctAnswer": ["A"]})
        items.append({"type": "简答", "title": "分包批次的问答。", "correctAnswer": ["完整问答答案。"]})
        bank = self.ingest(items)
        result = self.assert_cli_ok("export", self.bank_path, "--out", self.out,
                                    "--word-qa-mode", "include")
        self.assert_final_report(result)
        old_parts = [Path(path) for path in result["files"]["questionDocxParts"]]
        self.assertGreater(len(old_parts), 1, "本测试必须由真实最终ZIP尺寸触发分包")
        self.assertTrue(Path(result["files"]["manualQuestions"]).is_file())
        self.assertTrue(Path(result["files"]["fojiaoQaText"]).is_file())
        (self.out / "用户笔记.md").write_text("保留用户笔记", encoding="utf-8")
        # Work data can be corrected without replacing the registered original.
        bank["questions"] = [bank["questions"][0]]
        bank["questions"][0]["title"] = "只保留一题的最新工作数据。"
        write_json(self.bank_path, bank)
        lean = self.assert_cli_ok("export", self.bank_path, "--out", self.out,
                                  "--word-qa-mode", "manual")
        self.assert_final_report(lean)
        self.assertEqual(len(lean["files"]["questionDocxParts"]), 1)
        self.assertNotIn("manualQuestions", lean["files"])
        self.assertNotIn("manualQuestionsMarkdown", lean["files"])
        self.assertNotIn("fojiaoQaText", lean["files"])
        self.assertFalse((self.out / "CLI虚构课程问答题手动录入.md").exists())
        self.assertFalse((self.out / "CLI虚构课程问答题手动录入.txt").exists())
        self.assertFalse((self.out / "CLI虚构课程佛脚问答批量导入.txt").exists())
        for path in old_parts[1:]:
            self.assertFalse(path.exists(), path.name)
        self.assertEqual((self.out / "用户笔记.md").read_text(encoding="utf-8"), "保留用户笔记")

    def assert_hash_change_preserves_previous_export(self, changed):
        self.ingest()
        initial = self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        self.assertTrue(Path(initial["files"]["fojiaoQaText"]).is_file())
        before = self.snapshot()
        from questionbank import exporters
        original_export = exporters.export_bundle
        completed_inner = []

        def export_then_change_input(*args, **kwargs):
            result = original_export(*args, **kwargs)
            completed_inner.append(result)
            if changed == "bank":
                content = json.loads(self.bank_path.read_text(encoding="utf-8"))
                content["questions"][0]["explanation"] = "模拟外部并行修订工作题库。"
                write_json(self.bank_path, content)
            else:
                # A whitespace-only original edit still changes its source hash.
                with self.source.open("ab") as stream:
                    stream.write(b"\n")
            return result

        with patch("questionbank.exporters.export_bundle", side_effect=export_then_change_input):
            code, _, error = self.run_cli("export", self.bank_path, "--out", self.out,
                                          "--word-qa-mode", "manual")
        self.assertEqual(code, 1)
        self.assertIn("导出期间来源变化", error)
        self.assertEqual(len(completed_inner), 1, "应先完整生成暂存成果，再在外层哈希门失败")
        self.assertIn("fojiaoQaText", completed_inner[0]["files"])
        self.assertEqual(self.snapshot(), before)

    def test_json_channel_failure_after_word_generation_preserves_previous_deliverables(self):
        bank = self.ingest()
        initial = self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        self.assert_final_report(initial)
        before = self.snapshot()
        for question in bank["questions"]:
            if question["type"] == "简答":
                question["title"] = "尚未发布的新问答题干。"
        write_json(self.bank_path, bank)
        reached = []

        def fail_after_partial_json(bank, out, pandoc, report):
            reached.append(report["wordExport"]["manualQuestions"])
            self.assertTrue(Path(report["files"]["questionDocx"]).is_file())
            (Path(out) / "佛脚问答批量导入.txt").write_text('[{"部分写入":true}]', encoding="utf-8")
            raise RuntimeError("模拟问答 JSON 新渠道写入失败")

        with patch("questionbank.fojiao_qa.export_qa_json", side_effect=fail_after_partial_json):
            code, _, error = self.run_cli("export", self.bank_path, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("模拟问答 JSON 新渠道写入失败", error)
        self.assertEqual(reached, [1], "故障须发生在现有 Word 生成之后")
        self.assertEqual(self.snapshot(), before)

    def test_required_media_qa_retains_manual_source_while_other_qa_exports_as_json(self):
        from test_word_export import _png
        image = self.source.parent / "critical.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(_png((40, 90, 150)))
        self.ingest([
            {"type": "简答", "title": "关键图问答保留原手稿。", "correctAnswer": ["结合关键图的完整答案。"],
             "images": [{"path": str(image), "required": True, "field": "answer"}]},
            {"type": "简答", "title": "另一道纯文字问答继续。", "correctAnswer": ["文字答案完整。"]},
        ])
        original = self.source.read_bytes()
        result = self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        self.assert_final_report(result)
        self.assertEqual(result["wordExport"]["manualQuestions"], 2)
        self.assertEqual(result["fojiaoQaExport"]["selectedQuestions"], 2)
        self.assertEqual(result["fojiaoQaExport"]["exportedQuestions"], 1)
        self.assertEqual(result["fojiaoQaExport"]["deferredQuestions"], 1)
        rows = json.loads(Path(result["files"]["fojiaoQaText"]).read_text(encoding="utf-8"))
        self.assertEqual(rows[0]["题干"], "另一道纯文字问答继续。")
        for key in ["manualQuestions", "manualQuestionsMarkdown"]:
            retained = Path(result["files"][key]).read_text(encoding="utf-8")
            self.assertIn("关键图问答保留原手稿", retained)
            self.assertIn("结合关键图的完整答案", retained)
            self.assertIn("assets/", retained)
        attachments = result["wordExport"]["mediaAttachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual((self.out / attachments[0]["file"]).read_bytes(), image.read_bytes())
        self.assertEqual(self.source.read_bytes(), original)

    def test_json_media_matching_uses_original_source_identity_before_asset_hashing(self):
        from test_word_export import _png
        image = self.source.parent / "optional.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(_png((80, 120, 160)))
        bank = self.ingest([
            {"type": "简答", "title": "旧图位置缺标仍可导出可靠文字。",
             "correctAnswer": ["可靠完整答案。\n\n![非关键旧图](optional.png)"],
             "images": [{"path": str(image), "required": False}]},
            {"type": "简答", "title": "另一张未登记新图须延期。",
             "correctAnswer": ["须结合新图。\n\n![新图](critical-new.png)"],
             "images": [{"path": str(image), "field": "answer", "required": False}]},
        ])
        initial_source, initial_bank = self.source.read_bytes(), self.bank_path.read_bytes()
        result = self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        self.assert_final_report(result)
        qa = result["fojiaoQaExport"]
        self.assertEqual(qa["exportedQuestionIds"], [bank["questions"][0]["id"]])
        self.assertEqual(qa["deferredQuestionIds"], [bank["questions"][1]["id"]])
        self.assertIn("undeclared_inline_media", qa["deferred"][0]["reasons"])
        self.assertTrue(any(warning["questionId"] == bank["questions"][0]["id"]
                            and any(word in warning["message"] for word in ["位置", "定位", "标注"])
                            for warning in qa["warnings"]))
        rows = json.loads(Path(result["files"]["fojiaoQaText"]).read_text(encoding="utf-8"))
        self.assertIn("可靠完整答案", rows[0]["答案"])
        self.assertNotIn("optional.png", json.dumps(rows, ensure_ascii=False))
        normalized = json.loads(Path(result["files"]["normalizedJson"]).read_text(encoding="utf-8"))
        self.assertTrue(normalized["questions"][0]["images"][0]["path"].startswith("assets/"))
        self.assertNotEqual(normalized["questions"][0]["images"][0]["path"], str(image))
        self.assertEqual(self.source.read_bytes(), initial_source)
        self.assertEqual(self.bank_path.read_bytes(), initial_bank)

    def test_optional_bad_placement_only_keeps_word_pending_but_exports_qa_json(self):
        from test_word_export import _png
        image = self.source.parent / "optional.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(_png((30, 60, 90)))
        bank = self.ingest([
            {"type": "简答", "title": "可靠文字不依赖旧图片位置。", "correctAnswer": ["完整可靠答案。"],
             "images": [{"path": str(image), "required": False, "field": "option", "optionIndex": 0}]},
        ])
        result = self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        self.assert_final_report(result)
        qid = bank["questions"][0]["id"]
        self.assertEqual(result["wordExport"]["pendingQuestionIds"], [qid])
        self.assertEqual(result["wordExport"]["pendingQuestions"], 1)
        self.assertEqual(result["fojiaoQaExport"]["exportedQuestionIds"], [qid])
        self.assertFalse(result["fojiaoQaExport"]["deferredQuestionIds"])
        rows = json.loads(Path(result["files"]["fojiaoQaText"]).read_text(encoding="utf-8"))
        self.assertEqual(rows[0]["题干"], "可靠文字不依赖旧图片位置。")
        self.assertEqual(rows[0]["答案"], "完整可靠答案。")
        self.assertTrue(any(warning["questionId"] == qid
                            and any(word in warning["message"] for word in ["位置", "定位", "标注"])
                            for warning in result["fojiaoQaExport"]["warnings"]))

    def test_bank_changed_after_inner_export_preserves_previous_deliverables(self):
        self.assert_hash_change_preserves_previous_export("bank")

    def test_original_changed_after_inner_export_preserves_previous_deliverables(self):
        self.assert_hash_change_preserves_previous_export("source")

    def test_read_returns_old_bank_then_writes_new_bank_fails_before_export(self):
        self.ingest()
        self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        before = self.snapshot()
        from questionbank import cli, exporters
        original_read = cli.read_bank
        returned_ids = []

        def read_old_then_write_new(path):
            old = original_read(path)
            returned_ids.append(old["questions"][0]["id"])
            updated = json.loads(Path(path).read_text(encoding="utf-8"))
            updated["questions"][0]["title"] = "读取旧快照后落盘的新题干。"
            write_json(Path(path), updated)
            return old

        with patch("questionbank.cli.read_bank", side_effect=read_old_then_write_new), \
                patch("questionbank.exporters.export_bundle", wraps=exporters.export_bundle) as exporter:
            code, _, error = self.run_cli("export", self.bank_path, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("读取期间题库变化", error)
        self.assertEqual(len(returned_ids), 1)
        exporter.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertIn("读取旧快照后落盘的新题干", self.bank_path.read_text(encoding="utf-8"))

    def test_original_changes_after_registered_hash_read_cannot_reset_snapshot(self):
        bank = self.ingest()
        self.assert_cli_ok("export", self.bank_path, "--out", self.out)
        before = self.snapshot()
        original_source_digest = bank["sources"][0]["sha256"]
        changed = []
        from questionbank import exporters
        original_export = exporters.export_bundle

        def hash_old_then_change_source(path):
            digest = file_sha256(path)
            if Path(path).resolve() == self.source.resolve() and not changed:
                self.assertEqual(digest, original_source_digest)
                with self.source.open("ab") as stream:
                    stream.write(b"\n")
                changed.append(True)
            return digest

        with patch("questionbank.cli.file_sha256", side_effect=hash_old_then_change_source), \
                patch("questionbank.exporters.export_bundle", wraps=original_export) as exporter:
            code, _, error = self.run_cli("export", self.bank_path, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("导出期间来源变化", error)
        self.assertEqual(changed, [True])
        self.assertEqual(exporter.call_count, 1, "已验证的同份哈希须一直沿用到发布前核对")
        self.assertNotEqual(file_sha256(self.source), original_source_digest)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
