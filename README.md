# Document-Grounded Customer Service CLI

一个基于指定业务文档回答问题的 Python 命令行实验项目。它将 Markdown/PDF 文档解析为带位置的证据块，生成结构化业务契约，让模型分析问题，再由 Python 校验条件、规则版本、事实来源和原文引文。最终每题只输出 `answer` 或 `handoff` 的 JSONL 记录。

## 公开快照的范围

这是开发中的**脱敏源代码快照**，不是最终提交包，也不代表已经完成跨领域稳定性验收。仓库包含通用实现、离线测试，以及一组人工构造的“清禾图书馆”示例文档和问题。原始考核业务材料、参考答案、模型预测、契约缓存、实验日志与对话交接文件没有公开，因此原考核成绩不能仅凭本仓库独立复现。

## 运行

建议使用 Python 3.13。创建虚拟环境后安装依赖：

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
```

先运行不调用模型 API 的测试：

```bash
python -m pytest -q -m "not live_api"
```

在线演示需要自行设置 `DEEPSEEK_API_KEY` 环境变量。不要把密钥写入仓库。以下命令会向 DeepSeek 发送两份**合成示例文档**和 10 道示例问题，可能产生费用；业务日期固定为模拟日期 2030-04-15：

```bash
python app.py run \
  --docs evaluation/blind_domain/docs/manual_v1.md evaluation/blind_domain/docs/notice_v2.md \
  --questions evaluation/blind_domain/questions.jsonl \
  --output outputs/demo_predictions.jsonl \
  --as-of 2030-04-15
```

在 PowerShell 中可把换行续行符换为反引号，或把命令写在一行。`app.py` 还提供 `build-contract`、`inspect-contract` 和 `evaluate` 子命令；`evaluate` 需要自行提供本地参考答案 JSONL，本仓库不附带参考答案。

## 安全边界

- 只通过 `--docs` 指定要发送的文档，不要把含答案或私密材料的目录整体作为输入。
- 模型建议须经确定性校验；证据不足、条件缺失或超出范围时转人工。
- 输出文件、契约缓存和密钥文件被 `.gitignore` 排除；公开 Git 仓库不应作为保存凭据或原始业务材料的地方。

目前未附开源许可证；公开可见不等于授予复制、修改或再分发许可。
