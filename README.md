# NoteFlow：多模态笔记知识库整理多 Agent 系统

> **公开演示说明**：仓库中的 `demo_vault/` 是完全合成的数据集；个人 Obsidian
> 知识库、原始附件和运行记录不会发布。Render 部署使用该演示库，适合面试体验，
> 不适合作为多人生产服务。

一个可本地运行的多模态笔记整理工作台。用户可以上传截图/照片，也可以只填写几个关键词；系统会先检索公开资料，再通过串行流水线完成 OCR/内容提取、主题归类打标、信息压缩去重和 Markdown 归档草稿。确认后才会写入本地收件箱；向量知识库属于后续扩展。

本地工作区同时维护一套可被 Obsidian 直接打开的 AI 产品经理工作经验知识库。
项目内的 `ai-pm-knowledge-vault` skill 负责初始化、材料收录、知识查询、简历输出
与健康检查；知识图谱页可直接导入文档并拆成原子知识点。公开项目介绍见
[VibeAtlas 项目页](vibecoding-project.html)；本机知识库入口仍是 `[[01-知识库目录]]`。

公开克隆只包含源码、脱敏 skill 和 `demo_vault/`；本机个人知识库目录由
`.gitignore` 排除。要在本机使用自己的 Obsidian 根目录，按下文命令选择该目录；
要体验公开版本，按 `DEPLOYMENT.md` 使用 Render 的 `--vault-root demo_vault`。

## Obsidian 知识库与本地 Skill

在 Obsidian 中选择“打开本地仓库”，选中本项目根目录即可。仓库强制执行两项规则：内部笔记只使用无路径 `[[文件名]]` 双链；每次收录必须同时生成或更新一篇或多篇知识笔记、更新目录、追加唯一一行流水账。

初始化其他知识库：

```bash
python3 .agents/skills/ai-pm-knowledge-vault/scripts/init_vault.py /path/to/vault --profession "AI 产品经理"
```

检查当前知识库，或验收一次指定笔记的收录事务：

```bash
python3 .agents/skills/ai-pm-knowledge-vault/scripts/validate_vault.py .
python3 .agents/skills/ai-pm-knowledge-vault/scripts/validate_vault.py . --expect-note "本次新增知识点文件名"
```

## 架构

```text
图片 / 关键词 / 备注 / 文本回退
          |
          v
图片输入校验与预处理
          |
          v
LangGraph StateGraph（可选）或 NoteOrchestrator（无依赖回退）
          |
  关键词检索（有关键词时） -> OCR -> 分类打标 -> 压缩去重 -> Markdown 归档
          |
          v
  Markdown 归档草稿 + 标签索引 + 检索关键词 + 公开来源 URL + 事件记录
```

四个整理节点和可选检索节点共享 `NoteState`：`image_data`、`keywords`、`user_note`、`retrieved_text`、`retrieval_sources`、`raw_ocr_text`、`primary_category`、`sub_tags`、`condensed_text`、`archive_markdown`、`retrieval_keywords` 等字段。每个阶段都有输入/输出契约，失败时记录 `stage.failed`，可只重试指定阶段。

## 最小可运行闭环

- 笔记整理：`/api/notes/process` 生成草稿，`/api/notes/propose` 只生成待确认提案，用户确认后调用 `/api/notes/commit`，一次写入 `00-收件箱/`、更新目录并追加一行流水账。
- 知识图谱：`/api/knowledge/preview` 提取候选知识点和关键词，用户逐点选择“仅保存”或“保存并加入图谱”，确认后调用 `/api/knowledge/commit`；图谱只投影已确认的知识点。
- 收件箱整理：打开 `/inbox.html` 选择待整理笔记，可预览“新建知识点”或“追加已有笔记”的原文、结果和统一 diff；确认后才写入。新建项进入 `08-知识点/收件箱整理/`，追加项更新选定笔记；两种方式都会更新目录并追加一行流水账。预览后若目标或目录发生变化，提交会返回 `409`，需要重新预览。
- `process`、`propose`、`preview` 和取消操作都不写正式知识库；全部跳过知识点等同取消。重复提交使用 `run_id`/草稿编号幂等处理。

### 本轮改进闭环

- **OCR 人工校正**：整理结果同时保留原始 OCR、清洗文本、人工修订文本和当前有效文本。结果页会列出低置信度片段；提交修订后会重新执行分类、摘要和归档。原始 OCR 永远不被覆盖，提交时带 `expected_ocr_revision`，过期编辑返回 `409 ocr_revision_conflict`。
- **来源生命周期**：来源文档不做不可逆删除，使用“归档/恢复”隐藏或重新显示。归档只更新来源摘要、关联知识点状态、目录和流水账；原始附件字节保持不变。归档来源仍留在来源树中并显示“已归档”，默认图谱不渲染它的知识点。
- **知识点图谱**：节点只代表知识点，不代表来源文档或分类。知识点类型使用稳定颜色映射；旧笔记若只有通用 `atomic-claim`，会依据来源章节和证据文本推断视觉类型，不改写原笔记。密集或缩小时收起大部分标签，仅保留高连接度、悬停和选中节点标签，降低遮挡。

每完成一部分的最短验证方式：

```bash
python3 -m unittest tests.test_note_knowledge tests.test_improvement_endpoints -q
python3 -m unittest tests.test_source_lifecycle tests.test_source_graph_projection -q
python3 -m unittest tests.test_graph_frontend -q
```

## 快速启动

项目默认无第三方依赖，使用 Python 3 即可：

```bash
python3 server.py --port 4173
```

打开 <http://127.0.0.1:4173/> 使用 NoteFlow 整理台，打开 <http://127.0.0.1:4173/graph.html> 导入文档并浏览知识点图谱，或打开 <http://127.0.0.1:4173/inbox.html> 整理收件箱。图谱页选择文件后点击“提炼并审核”，逐点勾选并选择“仅保存”或“保存并加入图谱”，最后点击“确认收录”；取消或全部不选都不会写入。左侧按来源文档展开知识点；关系图只渲染 `type: knowledge-point`，支持搜索、类型筛选、拖拽、缩放、详情和原文追溯。

简历项目展示页：<http://127.0.0.1:4173/vibecoding-project.html>。这是一个不依赖 API 的静态页面，适合面试演示；本地服务重启后仍使用同一路径。若要让招聘方从互联网访问，需要把该静态页部署到 GitHub Pages、Netlify 或公司静态站点，`127.0.0.1` 本身不是公网地址。

整理台可以只上传一张图片直接点击“开始整理”，也可以只填写检索关键词，例如 `LangGraph、StateGraph、Agent 工作流`，系统会自动抓取公开网页摘要并把关键词串成一篇文本化笔记。关键词支持逗号、顿号、分号或换行分隔，最多 8 个。也可以选择/拖入图片，或在页面任意位置按 `Command+V`（Windows/Linux 使用 `Ctrl+V`）直接粘贴剪贴板截图。普通文字粘贴仍会进入当前文本框，不受图片粘贴监听影响。若直接双击打开 `index.html`，前端也会自动尝试连接本机 `4173` 服务。

知识图谱页支持 `.md`、`.markdown`、`.txt`、`.docx` 和 `.pdf`，单个文件最大 20 MB；扫描版 PDF 需要先提供 OCR 文本。所有文件在确认收录前都只保留在浏览器请求和内存预览中。

在 macOS 上，未配置文心凭证时会自动使用系统 Vision OCR，首次处理图片会编译本地辅助程序，可能需要约 10 秒；后续图片会直接复用缓存。如果没有图片，也可以在“文字回退输入”中点击“填入示例”体验完整流程。

关键词检索默认使用 Bing RSS，并保留每条来源的标题、摘要、URL 和检索时间；DuckDuckGo Instant Answer 与 Wikipedia 作为回退。网络不可用或没有结果时，流程仍会明确标记“无可用来源”，不会伪造 URL，归档笔记会保留检索提示供后续核验。

## API

读取当前 Obsidian 知识库的知识点、知识关系、来源分组和统计数据：

```bash
curl http://127.0.0.1:4173/api/knowledge/graph
```

导入一份文档。页面会把 MD、TXT、DOCX 和 PDF 都按 Base64 发送，以便原样保存文件字节；扫描版 PDF 需要先做 OCR：

```bash
base64 < your-document.md \
  | jq -Rs --arg filename "your-document.md" '{filename:$filename,base64:.}' \
  | curl -X POST http://127.0.0.1:4173/api/knowledge/ingest \
      -H 'Content-Type: application/json' --data-binary @-
```

知识图谱的审核式导入分两步：预览只返回候选点，不修改 vault；确认时提交用户选择。

```bash
curl -X POST http://127.0.0.1:4173/api/knowledge/preview \
  -H 'Content-Type: application/json' \
  -d '{"filename":"meeting.md","text":"先验证用户问题。\n\n再设计功能。"}'

curl -X POST http://127.0.0.1:4173/api/knowledge/commit \
  -H 'Content-Type: application/json' \
  -d '{"draft_id":"<preview.draft_id>","selected_points":[{"id":"<candidate.id>","save_mode":"save_and_graph"}]}'
```

从收件箱整理为知识点或补充已有笔记：

```bash
curl -X GET 'http://127.0.0.1:4173/api/vault/inbox'
curl -X POST http://127.0.0.1:4173/api/vault/promotion/propose \
  -H 'Content-Type: application/json' \
  -d '{"source_path":"00-收件箱/会议.md","mode":"new","target_name":"用户问题验证","title":"用户问题验证方法","content":"先验证用户问题，再设计功能。"}'
curl -X POST http://127.0.0.1:4173/api/vault/promotion/commit \
  -H 'Content-Type: application/json' \
  -d '{"proposal_id":"<proposal_id>","expected_hashes":<proposal.hashes>}'
```

`/api/vault/search?q=关键词` 提供本地全文搜索；`propose` 只返回差异，`commit` 才执行三文件事务。新建 promotion 会保留收件箱原文快照，并在 `08-知识点/收件箱整理/` 生成 `type: knowledge-point` 笔记。

来源归档/恢复也采用“预览后确认”的两步事务。`source_id` 可以使用图谱来源树返回的来源 id，也可以传来源摘要或原始附件路径：

```bash
curl -G http://127.0.0.1:4173/api/knowledge/source/inspect \
  --data-urlencode 'source_id=会议纪要-来源摘要'

curl -X POST http://127.0.0.1:4173/api/knowledge/source/propose \
  -H 'Content-Type: application/json' \
  -d '{"action":"archive","source_id":"会议纪要-来源摘要","operation_id":"archive-demo-1"}'

curl -X POST http://127.0.0.1:4173/api/knowledge/source/commit \
  -H 'Content-Type: application/json' \
  -d '{"proposal_id":"archive-demo-1","expected_hashes":<proposal.expected_hashes>}'

curl -X POST http://127.0.0.1:4173/api/knowledge/source/restore \
  -H 'Content-Type: application/json' \
  -d '{"source_id":"会议纪要-来源摘要"}'
```

`/api/knowledge/source/archive` 和 `/api/knowledge/source/restore` 是方便脚本调用的直接路由，服务端内部仍会先生成快照再提交。永久删除会明确返回 `409 permanent_delete_unsupported`。当前来源提案主要保存在服务进程内；服务重启后请重新 `propose`，不要复用旧的提案编号。

处理一份笔记：

```bash
curl -X POST http://127.0.0.1:4173/api/notes/process \
  -H 'Content-Type: application/json' \
  -d '{"image_data":"","image_name":"note.png","user_note":"LangGraph 学习笔记","ocr_text":"StateGraph 通过统一 State 编排多个 Agent。"}'
```

只用关键词生成知识笔记：

```bash
curl -X POST http://127.0.0.1:4173/api/notes/process \
  -H 'Content-Type: application/json' \
  -d '{"keywords":"LangGraph、StateGraph、Agent 工作流"}'
```

整理结果确认写入收件箱：

```bash
curl -X POST http://127.0.0.1:4173/api/notes/propose \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<run_id>"}'

curl -X POST http://127.0.0.1:4173/api/notes/commit \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<run_id>","approved_markdown":"<proposal.markdown>","expected_index_sha256":"<proposal.index_sha256>","expected_log_sha256":"<proposal.log_sha256>","note_name":"<proposal.note.name>"}'
```

`/api/notes/process` 和 `/api/notes/propose` 都不会写正式 vault；只有 `/api/notes/commit` 才完成“笔记、目录、流水账”三件套。重复提交同一 `run_id` 返回原笔记且不新增流水账。

OCR 校正接口（结果页的“应用修订并重新整理”按钮调用同一接口）：

```bash
curl -X POST http://127.0.0.1:4173/api/notes/runs/<run_id>/ocr/correct \
  -H 'Content-Type: application/json' \
  -d '{"corrected_text":"人工修订后的 OCR 文本","expected_ocr_revision":0}'
```

响应中的 `state.raw_ocr_text`/`state.original_ocr_text` 是不可变的原始识别结果，`state.corrected_ocr_text` 是人工修订，`state.effective_ocr_text` 是后续整理使用的文本。若多个标签页同时编辑，必须使用最新 revision 重新提交。

关键词模式的 `stage_order` 会变为 `retrieve`、`ocr`、`classify`、`summarize`、`archive`；响应中的 `outputs.retrieve.来源列表` 与 Markdown 的“公开检索来源”章节保留可点击 URL。普通图片/文本模式仍是四阶段。响应包含 `run_id`、`state`、`outputs`、`stages`、`events` 和 `archive_markdown`。阶段重试：

```bash
curl -X POST http://127.0.0.1:4173/api/notes/runs/<run_id>/stages/summarize/retry \
  -H 'Content-Type: application/json' -d '{}'
```

可用阶段别名：`retrieve`、`ocr`、`classify`、`summarize`、`archive`，也支持 `research`、`search` 和中文别名“检索”“内容提取”“打标”“精简”“归档”。

## 文心多模态接入

`note_knowledge.providers.WenxinMultimodalProvider` 是可选的 urllib 适配器。设置 `WENXIN_API_URL` 和 `WENXIN_API_KEY` 后，服务启动时会自动优先使用文心；未配置时不会发起网络请求，并在 macOS 上回退到系统 Vision OCR。也可以在代码中手动注入：

```python
from note_knowledge import NoteOrchestrator, OCRAgent
from note_knowledge.providers import WenxinMultimodalProvider

ocr = OCRAgent(WenxinMultimodalProvider(
    api_url="https://your-wenxin-endpoint",
    api_key="your-api-key",
))
service = NoteOrchestrator({"ocr": ocr})
```

OCR 提示词要求无法确认的文字使用 `[无法识别]`，不根据上下文臆造；模型返回 JSON 后仍会经过本地契约校验和噪声过滤。

## LangGraph 适配

安装 LangGraph 后，可以构建同样的四节点 StateGraph：

```python
from note_knowledge.graph import build_note_graph

graph = build_note_graph()
result = graph.invoke({
    "keywords": ["LangGraph", "StateGraph", "Agent 工作流"],
    "image_name": "note.png",
    "image_mime": "image/jpeg",
    "image_data": "...base64...",
    "user_note": "技术学习",
    "provided_text": "StateGraph 编排多 Agent。",
})
```

未安装 LangGraph 时，Web API 使用 `NoteOrchestrator`，不会影响本地演示和测试。

## 目录

```text
.agents/skills/ai-pm-knowledge-vault/
  SKILL.md        # AI PM 知识库维护契约
  scripts/        # 初始化与完整性校验
07-素材附件/
  原始资料/       # 逐字节保留的来源文件
  来源摘要/       # 来源与知识点映射
08-知识点/        # 按来源分组的原子知识点
note_knowledge/
  models.py       # NoteState、四类结果和事件契约
  agents.py       # OCR、分类、精简、归档 Agent
  retrieval.py    # 关键词公开检索与来源契约
  orchestrator.py # 串行状态机、重试、JSON 持久化
  providers.py    # 可选文心多模态适配器
  graph.py        # 可选 LangGraph StateGraph
server.py         # 静态页面 + 文档导入/图谱 API + 旧接口兼容
index.html        # NoteFlow 工作台
app.js            # 上传、处理、提案确认、结果预览和下载
styles.css        # 响应式工作台样式
graph.html        # Obsidian 风格知识图谱页面
graph.js          # 知识点图谱、来源树、审核导入、搜索筛选和交互
graph.css         # 图谱桌面、手机响应式样式
inbox.html        # 收件箱选择、整理提案与 diff 确认页
inbox.js          # 收件箱搜索、来源选择、promotion 提交
inbox.css         # 收件箱响应式样式
vendor/           # 本地 D3 与 Lucide 依赖及许可证
tests/            # 原项目兼容、知识库 Skill 与导入事务测试
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 可继续扩展

- 将 `retrieval_keywords` 和 YAML Front Matter 写入 Chroma、FAISS 或 Milvus；
- 增加图片旋转/清晰度检测和更细粒度的低置信度人工确认；
- 用 20–50 张脱敏样本评估 OCR 准确率、标签一致性、关键信息保留率和失败恢复率；
- 为来源生命周期提案增加跨服务重启的持久化存储；
- 将 `storage_dir` 指向独立数据目录，接入带回收站的本地历史记录。

当前明确先不做：向量检索/RAG、自动语义连边、云同步、多人协作、扫描版 PDF 的自动 OCR，以及来源的不可逆删除。先用原始证据保留、人工确认和可恢复归档验证日常工作流。

仓库中原有的 `career_copilot` 包和 `/api/analyze` 接口仍保留，便于兼容之前的测试与示例。
