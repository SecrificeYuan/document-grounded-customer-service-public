# Document-Grounded Customer Service CLI

一个根据指定业务文档回答问题的 Python 命令行实验项目。程序读取 Markdown/PDF 文档，生成结构化文档契约，让模型分析问题，再由 Python 校验业务条件、事实来源和证据引文。最终每道题输出 `answer`（回答）或 `handoff`（转人工）的 JSONL 记录。

## 公开快照的范围

这是开发中的**脱敏源代码快照**，不是最终提交包，也不代表已经完成跨领域稳定性验收。仓库包含通用实现、离线测试，以及人工构造的“清禾图书馆”示例文档和问题。原始考核业务材料、参考答案、模型预测、契约缓存、实验日志与对话交接文件均未公开。因此，可以用本仓库检查安装、离线测试和示例运行流程，但不能仅凭它复现原考核成绩。

## 1. 环境准备

需要 Git、Python 和能够安装 Python 包的网络环境。建议使用 Python 3.13；本仓库没有声明其他 Python 版本均受支持。先检查本机命令：

```text
git --version
python --version
```

克隆仓库后，进入项目根目录。以下命令中的相对路径都以这个目录为起点：

```text
git clone https://github.com/SecrificeYuan/document-grounded-customer-service-public.git
cd document-grounded-customer-service-public
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe --version
```

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python --version
```

`.venv` 是项目专用的虚拟环境。下面的命令直接调用其中的 Python，不要求激活虚拟环境，也不会误用系统 Python。如果只想运行程序、不运行测试，可以将安装命令中的 `requirements-dev.txt` 换成 `requirements.txt`。

### 依赖各做什么

依赖版本以仓库内的两个 requirements 文件为准：

| 依赖 | 文件中的版本 | 用途 |
| --- | --- | --- |
| `openai` | `3.16.2` | 通过 SDK 发起模型 API 请求；使用该 SDK 不代表必须使用 OpenAI 服务 |
| `pydantic` | `2.13.5` | 校验输入、契约、模型回复和最终输出的数据结构 |
| `pdfplumber` | `0.11.10` | 解析 PDF 文本与页面信息 |
| `pytest` | `9.1.1` | 运行离线自动测试，仅由 `requirements-dev.txt` 引入 |

## 2. 先运行离线测试

无需模型密钥，也不会向模型服务发送文档：

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m pytest -q -m "not live_api"
```

```bash
# Linux / macOS
.venv/bin/python -m pytest -q -m "not live_api"
```

这验证的是仓库中的离线代码测试，不是原考核题的评分。若出现 `No module named pytest`，通常是安装时使用了 `requirements.txt` 而非 `requirements-dev.txt`，或运行时没有使用 `.venv` 中的 Python。

## 3. 配置模型服务

离线测试不需要密钥；`run` 和 `build-contract` 是在线命令，需要模型服务提供的有效密钥。**密钥来自使用者实际选择的服务商，不一定是 DeepSeek。**但当前代码沿用以下 `DEEPSEEK_*` 环境变量名称，并默认连接 DeepSeek；变量名不等于对任意服务商的兼容承诺。

| 环境变量 | 必需？ | 当前默认值 / 含义 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 在线命令必需 | 使用者所选兼容服务的 API 密钥；不要写进仓库 |
| `DEEPSEEK_BASE_URL` | 可选 | 默认 `https://api.deepseek.com`；若换服务，需提供其 HTTPS API 根地址 |
| `DEEPSEEK_MODEL` | 可选 | 默认 `deepseek-flash`；若换服务，需填写其实际模型名 |

当前客户端调用的是 **Responses API**，请求包含 `reasoning.effort=high` 和 JSON Schema 格式约束。其他服务商即使接受类似的 API 密钥，也必须兼容这些请求与响应字段；仅替换密钥、地址或模型名**不保证能够运行**。本公开快照没有对其他服务商完成兼容性验收。不兼容时需要修改客户端适配代码，而不是把别家的密钥填入默认 DeepSeek 地址。请先确认服务商的文档与数据发送授权，在线调用可能产生费用。

在 Windows PowerShell 中，可以交互式输入密钥，只把它放进当前终端的环境变量，不写进 README、脚本或 Git：

```powershell
$secureKey = Read-Host "API key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
```

使用其他兼容服务时，还需在同一个终端设置其地址和模型名，例如 `$env:DEEPSEEK_BASE_URL = "https://<服务商的 API 根地址>"` 和 `$env:DEEPSEEK_MODEL = "<模型名>"`；尖括号是占位符，不可原样运行。Linux/macOS 可用终端或系统的安全密钥管理方式设置同名环境变量。运行结束后，PowerShell 可执行 `Remove-Item Env:DEEPSEEK_API_KEY` 清除当前终端变量。

## 4. 运行仓库自带的合成示例

示例输入是两份人工构造的图书馆文档和 10 道问题。**执行以下命令会把所指定的示例文档与问题发送给已配置的模型服务，并可能产生费用。**日期固定为模拟业务日期 `2030-04-15`。

```powershell
# Windows PowerShell；在项目根目录执行
.\.venv\Scripts\python.exe app.py run --docs evaluation/blind_domain/docs/manual_v1.md evaluation/blind_domain/docs/notice_v2.md --questions evaluation/blind_domain/questions.jsonl --output outputs/demo_predictions.jsonl --cache-dir . --as-of 2030-04-15
```

```bash
# Linux / macOS；在项目根目录执行
.venv/bin/python app.py run \
  --docs evaluation/blind_domain/docs/manual_v1.md evaluation/blind_domain/docs/notice_v2.md \
  --questions evaluation/blind_domain/questions.jsonl \
  --output outputs/demo_predictions.jsonl \
  --cache-dir . --as-of 2030-04-15
```

参数含义：`--docs` 指定允许发送的 Markdown/PDF 文档；`--questions` 指定逐行 JSON 的问题文件；`--output` 指定最终答案文件；`--cache-dir .` 将契约缓存根目录设为当前项目目录；`--as-of` 固定业务判断日期。不传 `--as-of` 时，程序使用中国标准时间的当天日期。建议在这个版本中始终写 `--cache-dir .`，以便 `run` 与 `inspect-contract` 指向同一处缓存。

成功完成批次时，终端打印 `processed=... answered=... handed_off=... item_failures=...`。首次运行若没有有效契约缓存，会先调用模型生成契约；生成或校验失败时，命令可能提前结束，不能仅凭缓存目录存在就认定答案文件已经生成。

### 使用自己的文档和问题

将示例命令中的 `--docs` 和 `--questions` 换成自己**获准发送给该模型服务**的文件。`--docs` 可列出多个 `.md` / `.pdf` 文件；不要直接传入含参考答案、密钥或无关私密资料的目录。问题文件采用 UTF-8 JSONL，每个非空行对应一个对象，例如：

```json
{"id":"Q01","session_id":"S01","question":"这里填写问题"}
```

每题应有唯一的 `id`。切换文档、模型或相关契约版本，可能生成不同的缓存键；旧缓存不等于新配置下已经完成验收。

## 5. 输出保存在哪里

所有相对路径都相对于**运行命令时的当前工作目录**。按上述示例从项目根目录运行后：

```text
document-grounded-customer-service-public/
├── evaluation/blind_domain/docs/           # 随仓库提供的示例输入文档
├── evaluation/blind_domain/questions.jsonl  # 随仓库提供的示例问题
├── outputs/demo_predictions.jsonl           # 运行后生成的最终答案
└── artifacts/contracts/
    └── <文档集哈希>/<缓存键>/
        ├── body.json                        # 结构化契约正文
        ├── manifest.json                    # 来源、模型、版本及哈希等元数据
        ├── review.json                      # 契约审核状态
        └── ready.json                       # 缓存完整性标记
```

查看前几条答案：

```powershell
Get-Content .\outputs\demo_predictions.jsonl -Encoding UTF8 -TotalCount 3
```

Linux/macOS 使用 `head -n 3 outputs/demo_predictions.jsonl`。答案文件每行是一个 JSON 对象，字段为 `id`、`decision`、`answer`、`reason_code` 和 `evidence`；`decision` 为 `answer` 时附证据，为 `handoff` 时转人工。再次使用同一个 `--output` 路径，会在整批结果校验完成后**替换旧文件**；需要保留多轮结果时请使用不同文件名。控制台汇总不会自动另存为报告。`outputs/`、`artifacts/contracts/`、`.env` 等路径已被 `.gitignore` 排除，但提交前仍应检查 `git status`，不要公开实际业务资料和密钥。

## 6. 其他命令与常见问题

`build-contract` 可为指定文档单独生成或读取契约；`inspect-contract` 可离线检查已经存在的有效缓存。例如，在成功运行上面的示例后：

```powershell
.\.venv\Scripts\python.exe app.py inspect-contract --docs evaluation/blind_domain/docs/manual_v1.md evaluation/blind_domain/docs/notice_v2.md --cache-dir . --as-of 2030-04-15
```

`evaluate` 需要**另行提供**预测文件和参考答案 JSONL，计算指标并将指标 JSON 打印到终端；本公开仓库不提供原考核参考答案，也不会自动保存评分报告。

- `DEEPSEEK_API_KEY is required`：在线命令所在终端未设置密钥；离线测试无需密钥。
- `No module named ...`：检查是否用 `.venv` 中的 Python 安装并执行命令。
- `no valid cached contract`：尚未为这组文档生成有效契约，或者文档/模型/缓存根目录与生成时不同。
- 只有 `handoff` 或出现 `item_failures`：逐题结果需要人工查看；有输出文件不等于答案正确，也不等于业务验收通过。
- 其他服务商返回请求格式错误：先核对其 Responses API、JSON Schema 和 `reasoning` 字段兼容性；本项目当前没有通用多供应商适配层。

## 安全与许可

只发送明确获准的文档和问题；不要把参考答案、原始私密材料或密钥作为模型上下文。模型提出的结论须经过确定性校验；证据不足、条件缺失或超出范围时应转人工。本仓库目前未附开源许可证；公开可见不等于授予复制、修改或再分发许可。

