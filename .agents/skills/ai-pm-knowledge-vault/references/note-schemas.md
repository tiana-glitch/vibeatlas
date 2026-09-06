# AI PM Note Schemas

Use the smallest schema that preserves evidence and makes the note reusable. Keep all note basenames unique.

## Routing table

Route notes consistently:

| Note type | Location |
| --- | --- |
| Unsorted capture | `00-收件箱/` |
| Company or role experience | `01-工作经历/公司与岗位/` |
| Period review | `01-工作经历/阶段复盘/` |
| Active or status-unknown project | `02-项目案例/进行中项目/` |
| Completed project | `02-项目案例/已完成项目/` |
| Capability | Update the durable page in `03-AI产品能力/` or add a uniquely named page there |
| Method, decision, or lesson | `04-经验与方法/` |
| Resume, STAR, interview, or self-introduction output | `05-求职材料/` |
| Metric, contribution, or proof | `06-成果与证据/` |
| Image or original source attachment | `07-素材附件/图片/` or `07-素材附件/原始资料/` |
| Atomic knowledge point extracted from a document | `08-知识点/` |

Reserve `01-工作经历/工作经历总览.md` and `02-项目案例/项目案例总览.md` as the only Markdown files directly inside those parent directories.

## Common metadata

Use YAML frontmatter on durable notes:

```yaml
---
type: project | work-experience | capability | method | decision | evidence | career-output | inbox | knowledge-point
status: 进行中 | 已完成 | 待整理 | 快照
created: YYYY-MM-DD
updated: YYYY-MM-DD
evidence_status: 已核验 | 部分核验 | 待核验
confidentiality: 私密 | 已脱敏 | 可公开
tags: []
---
```

Refresh `updated` only when knowledge content changes. Use `待补充` instead of inventing missing facts.

## Knowledge point

Use a knowledge point when one source document contains several ideas that should be found, linked, or reused independently. Keep one claim, method, decision, lesson, or definition per note; keep broader project narratives in project notes.

Required frontmatter beyond the common metadata:

```yaml
source_document: "Grounded document title"
source_file: "unique-source-filename.pdf"
```

- `source_document` is the supplied document title, or its grounded filename when no title is available.
- `source_file` is the exact unique basename, including extension, of the byte-for-byte source saved under `07-素材附件/原始资料/`. Do not put a directory path in this field.
- Include `[[unique-source-filename.pdf]]` in the note's source section. Raw Markdown sources are linked with their `.md` extension and remain unmodified evidence; their internal links are not vault knowledge links.
- Record an exact excerpt and page, heading, paragraph, timestamp, or other locator when supplied. Mark unavailable location or uncertain interpretation `待补充` or `待核验`.
- Separate the sourced statement from interpretation, applicable boundary, and related notes.
- Give every knowledge point a descriptive basename that is unique across all managed notes. Qualify collisions by domain, source, or context rather than overwriting.

Recommended body:

1. Atomic conclusion
2. Source and evidence, including the source-file wikilink and locator
3. Interpretation and applicable boundary
4. Open questions or evidence gaps
5. Related basename wikilinks

For one-document-to-many-points ingestion, update `01-知识库目录.md` twice: one row in **按来源** maps the source-file wikilink to every derived point; one row per point in **按知识点** records a retrieval summary and the same source-file wikilink. Refresh existing rows instead of duplicating them.

## Work experience

Capture:

1. Company or business context, with sensitive names redacted when needed
2. Role, scope, dates, collaborators, and responsibility boundary
3. Goals and constraints
4. Representative projects with `[[文件名]]` links
5. Decisions and tradeoffs
6. Outcomes, metrics, and evidence state
7. Capability growth and lessons
8. Resume-ready statements that remain faithful to evidence

## Project case

Capture:

1. Background and user problem
2. Goal and success criteria
3. User and usage scenario
4. Constraints and risks
5. Product or workflow design
6. Key decisions and rejected alternatives
7. Personal actions and collaboration boundary
8. Results and evidence
9. Problems, lessons, and next iteration
10. Related capability, method, evidence, and career-output notes

For an AI product, also record model role, input/output contract, failure modes, evaluation set, evaluation metrics, human review, latency/cost constraints, and privacy considerations when known.

## Capability note

Maintain a long-lived capability page rather than one summary per source:

- Definition and boundary
- Reusable method
- Linked project evidence
- Common failure modes
- Interview expression
- Current gaps and next practice

Recommended AI PM capability areas: user and demand discovery, product design, LLM and multimodal understanding, data and evaluation, project delivery, prompt and agent workflow design, safety and privacy.

## Method, decision, and lesson

Record the situation, signal, options, decision, rationale, observed result, applicable boundary, and related cases. For a conflict, preserve both claims and add:

```markdown
> 状态：有争议
> 分歧来源与尚待确认的证据。
```

For superseded knowledge, add:

```markdown
> 状态：已过时（YYYY-MM-DD）
> 被什么新事实替代，以及旧结论仍适用的边界。
```

## Evidence note

Record the claim supported, evidence type, source location, observed date, confidentiality, exact excerpt or metric, and linked project. Store attachments under `07-素材附件/` and link by basename only.

Never expose a private internal URL, access token, customer identifier, or non-public metric in a public-facing note.

## STAR story

- **Situation**: context and stakes
- **Task**: the user's owned objective and constraints
- **Action**: decisions and actions personally taken
- **Result**: outcome with evidence status
- **Reflection**: learning and what would change next time
- **Source notes**: basename wikilinks only

## Resume bullet

Use this shape:

```text
[Action] + [problem/scope] + [method or decision] + [result] + [evidence qualifier when needed]
```

Prefer one or two lines. Do not add a number merely to make the bullet look stronger. Keep a linked source-note list and a `last_reviewed` date on the containing career-output page.

## Ingest log row

Append exactly one row per ingest to `02-更新流水账.md`. When one document creates or updates several knowledge points, list all touched notes in the same row:

```markdown
| YYYY-MM-DD HH:mm | 新增/更新/归档/检查 | [[知识点一]]、[[知识点二]] | [[唯一来源文件.pdf]] | 完成或待核验 |
```

The row must link every knowledge note written or updated in the same transaction. An attachment alone is not sufficient, and multiple points from one source must not create multiple log rows.
