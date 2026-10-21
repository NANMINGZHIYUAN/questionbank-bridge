"""Temporary-vault tests for conservative, private and incremental publishing."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from questionbank.core import file_sha256, normalize
from questionbank.knowledge import KnowledgePublishError, publish_knowledge
import questionbank.knowledge as knowledge


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.folder = Path(self.temp.name)
        self.vault = self.folder / "本地知识库"
        self.source = self.folder / "original.json"
        self.source.write_text('{"original":"unchanged"}', encoding="utf-8")
        source = {"id": "src-test", "path": str(self.source.resolve()),
                  "sha256": file_sha256(self.source), "format": "json",
                  "url": "https://mooc1.chaoxing.com/work?secret=private"}
        self.bank = normalize({"items": [{"id": 1, "type": "单选", "title": "秘密原题不得公开",
            "options": [{"label": "A", "text": "完整原答案不得公开"}, {"label": "B", "text": "另一项"}],
            "correctAnswer": ["A"], "selectedAnswer": ["B"]}]}, "演示课程", source)
        qid = self.bank["questions"][0]["id"]
        self.bank["questions"][0]["topicIds"] = ["t-one"]
        self.bank["topics"] = [{"id": "t-one", "title": "基础概念", "summary": "阶段目标",
                                "explanation": "**概念：** 已有解释。\n\n参考：[官方资料](https://example.edu/doc)。",
                                "questionIds": [qid], "cards": [{"id": "c-one", "prompt": "怎样理解？",
                                "answer": "依据现有解释判断。", "questionIds": [qid]}]}]

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, bank=None):
        return publish_knowledge(bank or self.bank, self.vault, "2026-10-10")

    def file_state(self):
        return {p.relative_to(self.folder): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.folder.rglob("*") if p.is_file()}

    def test_first_publish_private_sources_titles_and_navigation(self):
        original = (self.source.read_bytes(), self.source.stat().st_mtime_ns)
        report = self.publish()
        topic = Path(report["courseDirectory"]) / report["topicFiles"]["t-one"]
        self.assertEqual(topic.stem, "01-基础概念")
        body = topic.read_text(encoding="utf-8")
        self.assertIn("# " + topic.stem, body)
        self.assertEqual(body.count("> 返回："), 1)
        self.assertIn("## 本课复习", body)
        self.assertIn("https://example.edu/doc", body)
        self.assertIn("基础概念", Path(report["map"]).read_text(encoding="utf-8"))
        self.assertNotIn("已有解释", Path(report["map"]).read_text(encoding="utf-8"))
        self.assertEqual(original, (self.source.read_bytes(), self.source.stat().st_mtime_ns))
        self.assertTrue(report["sourceConservation"][0]["verified"])
        self.assertIn("/个人/", (self.folder / ".gitignore").read_text(encoding="utf-8"))
        private_index = json.loads(Path(report["questionIndex"]).read_text(encoding="utf-8"))
        self.assertEqual(private_index["questions"][0]["title"], "秘密原题不得公开")
        self.assertNotIn("selectedAnswer", json.dumps(private_index))
        public = "\n".join(p.read_text(encoding="utf-8") for p in self.vault.rglob("*.md"))
        for secret in (str(self.source), "mooc1.chaoxing", "secret=private", "秘密原题不得公开", "完整原答案不得公开"):
            self.assertNotIn(secret, public)
        self.assertIn("private-source:QB-" + self.bank["sources"][0]["id"], public)
        self.assertIn(file_sha256(self.source), public)

    def test_identical_publish_is_noop_preserving_all_mtimes(self):
        self.publish()
        before = self.file_state()
        report = self.publish()
        self.assertEqual(report["written"], [])
        self.assertEqual(before, self.file_state())
        self.assertEqual((self.vault / "log.md").read_text(encoding="utf-8").count("## [2026-10-10]"), 1)

    def test_learning_stages_survive_partial_topic_updates(self):
        extended = deepcopy(self.bank)
        extended['knowledgeStages'] = [{'title': '第一阶段', 'topicIds': ['t-one'],
                                       'goal': '了解概念之间的关系。',
                                       'checkpoint': '能完成本课的复习检查。'}]
        result = self.publish(extended)
        mapped = Path(result['map']).read_text(encoding='utf-8')
        self.assertIn('### 第一阶段', mapped)
        update = deepcopy(self.bank)
        update['topics'][0]['explanation'] = '本批补充的概念解释。'
        revised = self.publish(update)
        self.assertEqual(mapped, Path(revised['map']).read_text(encoding='utf-8'))

    def test_missing_learning_stage_reference_stops_all_writes(self):
        invalid = deepcopy(self.bank)
        invalid['knowledgeStages'] = [{'title': '第一阶段', 'topicIds': ['missing-topic'],
                                      'goal': '阶段目标。', 'checkpoint': '过关条件。'}]
        before = self.file_state()
        with self.assertRaisesRegex(KnowledgePublishError, 'missing topics'):
            self.publish(invalid)
        self.assertEqual(before, self.file_state())

    def test_local_update_keeps_numbering_other_pages_and_handwritten_text(self):
        extended = deepcopy(self.bank)
        topic_two = deepcopy(extended["topics"][0])
        topic_two.update(id="t-two", title="后续主题", explanation="第二主题正文。")
        topic_two["cards"][0]["id"] = "c-two"
        extended["topics"].append(topic_two)
        report = self.publish(extended)
        folder = Path(report["courseDirectory"])
        first = folder / "01-基础概念.md"
        second = folder / "02-后续主题.md"
        second_before = (second.read_bytes(), second.stat().st_mtime_ns)
        with first.open("a", encoding="utf-8") as stream:
            stream.write("\n我的手写笔记：保留。\n")
        index = self.vault / "index.md"
        with index.open("a", encoding="utf-8") as stream:
            stream.write("\n另一个课程的索引。\n")
        update = deepcopy(self.bank)
        update["topics"][0]["title"] = "新措辞仍保留编号文件"
        update["topics"][0]["explanation"] = "修订后的已有知识。"
        updated = self.publish(update)
        self.assertEqual(updated["topicFiles"]["t-one"], "01-基础概念.md")
        self.assertEqual(second_before, (second.read_bytes(), second.stat().st_mtime_ns))
        self.assertIn("我的手写笔记：保留", first.read_text(encoding="utf-8"))
        self.assertIn("修订后的已有知识", first.read_text(encoding="utf-8"))
        self.assertIn("另一个课程的索引", index.read_text(encoding="utf-8"))
        self.assertIn("02-后续主题", Path(updated["map"]).read_text(encoding="utf-8"))

    def test_unknown_existing_page_is_not_overwritten(self):
        existing = self.vault / "03-资源库" / "课程" / "演示课程" / "01-基础概念.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("# 01-基础概念\n用户原有内容", encoding="utf-8")
        before = self.file_state()
        with self.assertRaises(KnowledgePublishError):
            self.publish()
        self.assertEqual(before, self.file_state())

    def test_source_change_stops_before_writing(self):
        self.source.write_text("externally changed", encoding="utf-8")
        with self.assertRaisesRegex(KnowledgePublishError, "SHA-256"):
            self.publish()
        self.assertFalse(self.vault.exists())

    def test_source_change_mid_transaction_rolls_back_derived_files(self):
        original_atomic = knowledge._atomic_replace
        changed = False
        def write(path, data):
            nonlocal changed
            original_atomic(path, data)
            if not changed:
                changed = True
                self.source.write_text("external modification", encoding="utf-8")
        with patch.object(knowledge, "_atomic_replace", side_effect=write):
            with self.assertRaisesRegex(KnowledgePublishError, "changed during"):
                self.publish()
        self.assertFalse(list(self.vault.rglob("*.md")))
        self.assertFalse(list((self.folder / "个人").rglob("*.json")))
        self.assertEqual(self.source.read_text(encoding="utf-8"), "external modification")

    def test_unsafe_paths_private_root_and_empty_topics(self):
        for field in ("../outside", "a/b", "CON", " a "):
            bank = deepcopy(self.bank)
            bank["course"] = field
            with self.assertRaises(KnowledgePublishError):
                self.publish(bank)
        with self.assertRaises(KnowledgePublishError):
            publish_knowledge(self.bank, self.vault, "2026-10-10", self.vault / "private")
        empty = deepcopy(self.bank)
        empty["topics"] = []
        empty["questions"][0]["topicIds"] = []
        self.assertEqual(self.publish(empty)["status"], "no_topics")
        self.assertFalse(self.vault.exists())

    def test_failure_rolls_back_existing_files_including_mtimes(self):
        self.publish()
        before = self.file_state()
        bank = deepcopy(self.bank)
        bank["topics"][0]["explanation"] = "需要更新的正文。"
        original_atomic = knowledge._atomic_replace
        count = 0
        def write(path, data):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("simulated disk failure")
            original_atomic(path, data)
        with patch.object(knowledge, "_atomic_replace", side_effect=write):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.publish(bank)
        self.assertEqual(before, self.file_state())

    def test_concurrent_shared_page_edit_preserved_and_own_edits_rolled_back(self):
        self.publish()
        topic = self.vault / "03-资源库" / "课程" / "演示课程" / "01-基础概念.md"
        topic_before = (topic.read_bytes(), topic.stat().st_mtime_ns)
        index = self.vault / "index.md"
        old_index = index.read_text(encoding="utf-8")
        bank = deepcopy(self.bank)
        bank["topics"][0]["explanation"] = "新正文。"
        original_atomic = knowledge._atomic_replace
        changed = False
        def write(path, data):
            nonlocal changed
            original_atomic(path, data)
            if not changed:
                changed = True
                index.write_text(old_index + "\n其它会话刚写入。\n", encoding="utf-8")
        with patch.object(knowledge, "_atomic_replace", side_effect=write):
            with self.assertRaisesRegex(KnowledgePublishError, "concurrently"):
                self.publish(bank)
        self.assertIn("其它会话刚写入", index.read_text(encoding="utf-8"))
        self.assertEqual(topic_before, (topic.read_bytes(), topic.stat().st_mtime_ns))

    def test_learning_links_local_paths_are_redacted_in_authored_text(self):
        bank = deepcopy(self.bank)
        bank["topics"][0]["explanation"] += "\n" + str(self.source) + "\n" + bank["sources"][0]["url"]
        bank["topics"][0]["explanation"] += "\nhttps://passport2.chaoxing.com/private?id=1\nfile:///C:/private/file.pdf"
        report = self.publish(bank)
        public = "\n".join(p.read_text(encoding="utf-8") for p in self.vault.rglob("*.md"))
        self.assertNotIn(str(self.source), public)
        self.assertNotIn("chaoxing.com", public)
        self.assertNotIn("file://", public)
        self.assertIn("https://example.edu/doc", public)
        self.assertTrue(Path(report["sourceMapping"]).exists())

    def test_original_source_cannot_overlap_a_shared_output(self):
        self.vault.mkdir()
        original_index = self.vault / "index.md"
        original_index.write_text("# 原始资料，绝不能改写", encoding="utf-8")
        bank = deepcopy(self.bank)
        bank["sources"][0]["path"] = str(original_index.resolve())
        bank["sources"][0]["sha256"] = file_sha256(original_index)
        before = self.file_state()
        with self.assertRaisesRegex(KnowledgePublishError, "overlaps an original"):
            self.publish(bank)
        self.assertEqual(before, self.file_state())

    def test_blank_authored_explanation_does_not_create_fake_page(self):
        bank = deepcopy(self.bank)
        bank["topics"][0]["explanation"] = ""
        with self.assertRaisesRegex(KnowledgePublishError, "authored topic"):
            self.publish(bank)
        self.assertFalse(self.vault.exists())

    def test_uncertain_reference_does_not_publish_a_determined_review_answer(self):
        bank = deepcopy(self.bank)
        bank["questions"][0]["issues"].append({"code": "requires_content_review", "message": "需核实"})
        report = self.publish(bank)
        topic = Path(report["courseDirectory"]) / "01-基础概念.md"
        self.assertEqual(report["skippedUnsafeCards"], ["c-one"])
        self.assertNotIn("依据现有解释判断", topic.read_text(encoding="utf-8"))

    def test_frontmatter_is_legal_and_creation_date_is_stable_across_updates(self):
        report = self.publish()
        topic = Path(report["courseDirectory"]) / "01-基础概念.md"
        for path, kind in ((topic, "concept"), (Path(report["map"]), "overview"),
                           (Path(report["sourceList"]), "source-summary")):
            text = path.read_text(encoding="utf-8")
            front = text.split("---", 2)[1]
            self.assertIn("type: " + kind, front)
            for field in ("tags:", "date: 2026-10-10", "source:", "status: active"):
                self.assertIn(field, front)
            self.assertNotIn(str(self.source), front)
        bank = deepcopy(self.bank)
        bank["topics"][0]["explanation"] = "下次更新的正文。"
        publish_knowledge(bank, self.vault, "2026-10-11")
        self.assertIn("date: 2026-10-10", topic.read_text(encoding="utf-8"))

    def test_cards_fall_back_to_topic_references_and_reject_missing_or_unknown_refs(self):
        for mode in ("fallback_safe", "fallback_unsafe", "empty", "unknown"):
            with self.subTest(mode=mode):
                bank = deepcopy(self.bank)
                bank["course"] = "卡片" + mode
                card = bank["topics"][0]["cards"][0]
                card["questionIds"] = []
                if mode == "fallback_unsafe":
                    bank["questions"][0]["issues"].append({"code": "requires_content_review", "message": "需核实"})
                elif mode == "empty":
                    bank["topics"][0]["questionIds"] = []
                elif mode == "unknown":
                    card["questionIds"] = ["q-unknown"]
                report = self.publish(bank)
                topic = Path(report["courseDirectory"]) / "01-基础概念.md"
                if mode == "fallback_safe":
                    self.assertIn("依据现有解释判断", topic.read_text(encoding="utf-8"))
                    self.assertEqual(report["skippedUnsafeCards"], [])
                else:
                    self.assertNotIn("依据现有解释判断", topic.read_text(encoding="utf-8"))
                    self.assertEqual(report["skippedUnsafeCards"], ["c-one"])


if __name__ == "__main__":
    unittest.main()
