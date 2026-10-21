# Questionbank Bridge（题库桥）

一个本地运行的中文题库整理工具：摄入 JSON、文本、Markdown、DOCX 和 PDF，保留来源与待核实问题，导出 Markdown、Word、Anki `.apkg` 及问答导入用 JSON 文本。仓库只含代码、自动化测试和虚构示例；不包含课程原件、个人作答或知识库内容。

工具不会自动生成知识解释，也不会把缺失或冲突的答案当作确定答案制卡。图片和扫描版 PDF 需要人工核对文字稿。导出报告中的 `actualAnkiImportVerified` 和 `actualFojiaoImportVerified` 是 `false`，平台实际导入需要用户自行检验。

## 安装

需要 Python 3.10+ 和可从 `PATH` 运行的 [Pandoc](https://pandoc.org/installing.html)。在仓库目录运行：

```sh
python -m venv .venv
# 激活虚拟环境后：
python -m pip install -e .
```

若 Pandoc 不在 `PATH`，每个 `ingest`、`sample`、`export` 命令都可使用 `--pandoc` 指定其可执行文件路径。普通摄入 JSON 不需要 Pandoc，但完整导出需要它。PDF 文本提取使用 `pypdf`；扫描页仍需人工核对。

## 虚构示例

```sh
python examples/make_demo.py
python -m questionbank validate 工作区/演示/bank.json
python -m questionbank sample 工作区/演示/bank.json --out 工作区/演示/sample --limit 5
python -m questionbank export 工作区/演示/bank.json --out 工作区/演示/export
```

示例脚本写入被 Git 忽略的 `工作区/`。`sample` 生成 5–10 道题的小样；`export` 生成整批文件。完整导出默认只写 `--out` 目录，命令返回的 `knowledgePublication.status` 为 `skipped`。如需将已经人工编写的知识点发布到自己的本地知识库，显式传入 `--knowledge-vault PATH`，并先核对待发布内容。该选项也会在知识库的同级 `个人/题库/` 目录保存私有索引与发布状态。

对真实资料，建议先执行 `ingest`，检查 `intake-report.json` 和统一题库中的疑点，再小样导出，最后按需要整批导出：

```sh
python -m questionbank ingest input.json --course 演示课程 --out work/intake
python -m questionbank sample work/intake/bank.json --out work/sample --limit 5
```

`ingest` 不改写输入，记录原件哈希；已有题库可通过 `--existing` 增量合并。摄入后的 `bank.json` 包含本机来源路径，只适合留在本地。导出目录也可能包含题目原文及元数据，公开分享前须逐项审查。不要把真实题库、个人答案或私有来源上传到仓库。

## 开发与范围

```sh
python -m unittest discover -s tests -v
```

核心模块位于 `questionbank/`，测试在 `tests/`。Good Review JSON 可作为一种输入格式；本项目与该平台没有官方关联，也未复制它的代码。Anki 包由 `genanki` 生成。许可证见 [LICENSE](LICENSE)。
