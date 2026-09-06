---
name: ai-pm-knowledge-vault
description: "Build and maintain an Obsidian knowledge vault for an AI product manager. Use when initializing a personal knowledge base; ingesting work experience, project material, screenshots, articles, meeting notes, reflections, or a document that should be split into multiple reusable knowledge points; updating the vault index and operation log; preparing resume or interview material from evidence; querying existing notes; or auditing Obsidian wikilinks and vault health. Trigger on requests such as '收录这篇', '把文档拆成知识点', '整理工作经验', '更新知识库', '做项目复盘', '生成 STAR/简历素材', or '检查知识库'."
---

# AI PM Knowledge Vault

Maintain an evidence-grounded, Obsidian-native knowledge base that compounds into work reviews, interview stories, and resume material.

## Enforce the invariants

Apply these rules without exception:

1. Link another note only with a basename wikilink such as `[[文件名]]`. Never put a directory slash in a wikilink and never use a local Markdown path link between notes.
2. Finish every ingest as one transaction: write or update one or more knowledge notes, update `[[01-知识库目录]]`, and append exactly one row to `[[02-更新流水账]]`. A document split into multiple knowledge points is still one ingest and gets one log row containing every touched note.
3. Keep every managed note basename unique across the vault. Give each saved source file a unique filename so basename wikilinks are unambiguous; never overwrite an unrelated note or source file on collision.
4. Ground dates, metrics, direct quotes, and personal contributions in supplied material. Mark missing facts as `待补充` or `待核验`; never invent resume evidence.
5. Preserve contradictory or superseded claims. Mark them `有争议` or `已过时` with an explanation instead of silently rewriting history.
6. Treat company-confidential material as private by default. Redact names, internal links, identifiers, and exact metrics before producing a public resume version.

## Locate the vault

Use the directory explicitly selected by the user. Otherwise, use the current working directory only when it clearly contains `00-知识库说明.md` or `.obsidian/`. Do not scan or mutate unrelated directories.

Exclude `.agents/`, `.git/`, `.obsidian/`, `.note_runs/`, and application source code from knowledge-note search unless the user explicitly asks about implementation.

## Choose the operation

- **Initialize**: create a new vault structure after the user approves the proposed directory.
- **Ingest**: turn new material into one or more durable notes and complete the single transaction.
- **Query**: answer from existing notes without changing files.
- **Career output**: derive a review, STAR story, interview answer, self-introduction, or resume bullet from existing evidence.
- **Audit**: check filenames, links, index coverage, and ingest completeness.

## Initialize a vault

When the user's needs are not already known, ask one conversational question at a time about profession, what they will store, what they will use it for, and what they most fear losing. Propose a directory tree and wait for explicit approval before creating files.

After approval, run:

```bash
python3 <skill-dir>/scripts/init_vault.py <vault-root> --profession "AI 产品经理"
python3 <skill-dir>/scripts/validate_vault.py <vault-root>
```

Create only missing files. Never overwrite existing notes during initialization. Open `assets/vault-template/` only when changing the reusable vault template.

## Ingest material

Treat pasted text, a URL, a file, a screenshot, or a spoken recollection as source material. For a supplied document, decide whether it is one project/experience note or contains several independently reusable claims, methods, decisions, lessons, or definitions that should become atomic knowledge points.

1. Read `01-知识库目录.md`, the relevant overview page, and nearby notes found through keyword and synonym search.
2. Extract source facts, the user's actions, decisions, results, evidence, lessons, and unresolved gaps. Separate facts from interpretation.
3. Select note types and locations using `references/note-schemas.md`. Put project cases in a status child directory and atomic document-derived notes in `08-知识点/`.
4. Reuse an existing note when the material belongs to the same project or thesis. Give each new note a descriptive, globally unique basename; add a grounded qualifier when a title collides.
5. For document decomposition, preserve the supplied file byte-for-byte under `07-素材附件/原始资料/` with a unique basename. For pasted or fetched text, save a verbatim source capture there. Never edit source evidence to make validation pass; raw Markdown in this directory is evidence, not a managed knowledge note.
6. Write one `type: knowledge-point` note per independently reusable idea. Every point must name `source_document`, name the exact `source_file` basename, link that file as `[[文件名.扩展名]]`, and quote or locate the supporting evidence. Do not turn unsupported interpretation into source fact.
   Automatic decomposition structures the source; it does not verify its claims. Keep new points `evidence_status: 待核验` unless independent evidence supplied by the user supports a stronger status.
7. Update `01-知识库目录.md` in both knowledge-point views: refresh one source row linking the source file to all points from it, and add or refresh one row per knowledge point with its retrieval summary and source link.
8. Append exactly one table row to `02-更新流水账.md` for the whole ingest. Put every written or updated knowledge-note wikilink in that row and the source-file wikilink in the source column.
9. Validate the transaction, repeating `--expect-note` for every note touched by the same ingest:

```bash
python3 <skill-dir>/scripts/validate_vault.py <vault-root> \
  --expect-note "<知识点一>" --expect-note "<知识点二>"
```

All repeated `--expect-note` values represent one ingest and must resolve in one and only one log row. Do not claim completion while validation reports an error. Even thin, duplicate, or low-value input must produce or update a note, touch the directory, and add one log row; do not introduce a "no material" exception.

### Inbox promotion

The local `/inbox.html` workflow is also a reviewed ingest boundary. `new`
promotions create a `type: knowledge-point` note under `08-知识点/收件箱整理/`,
copy the selected inbox note byte-for-byte into `07-素材附件/原始资料/`, and
map that snapshot to the point in both index views. `append` promotions update
the selected existing note and add one trace row to the index; they do not
silently overwrite a changed target. Both modes require the same single log row,
and a changed source, target, target directory, index, or log invalidates the
preview and requires a new proposal.

## Query the vault

1. Read `01-知识库目录.md` for candidate notes.
2. Search all managed notes with the user's terms, aliases, and synonyms before concluding nothing is present.
3. Synthesize from the notes and state any evidence gaps.
4. Cite vault notes with `[[文件名]]`.
5. Do not write files for a plain query. If the user asks to save the answer, treat it as a new ingest transaction.

## Produce career material

Read the relevant project, work-experience, capability, and evidence notes. Use the formats in `references/note-schemas.md`.

- Keep resume bullets concise: action, problem, method, result, evidence state.
- Use STAR for interview stories while preserving the user's actual contribution boundary.
- Do not convert team outcomes into personal ownership without evidence.
- Keep `待核验` beside unsupported metrics until the user supplies proof.
- Mark generated resume and interview pages as snapshots with a last-reviewed date.
- Complete the three-file transaction when saving any generated output to the vault.

## Audit the vault

Run:

```bash
python3 <skill-dir>/scripts/validate_vault.py <vault-root>
```

Fix path-style wikilinks, local Markdown note links, duplicate basenames, and uniquely resolvable broken links. Report ambiguous links and evidence conflicts instead of guessing. After an audit that changes files, append one audit row to `02-更新流水账.md`.

## Report completion

State every note written or updated, the source and knowledge-point directory entries changed, the single log row appended, and the validation result. Mention unresolved evidence or confidentiality gaps explicitly.

This workflow is an original Obsidian adaptation inspired by [Karpathy's LLM Wiki concept](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) and the MIT-licensed [Astro-Han community skill](https://github.com/Astro-Han/karpathy-llm-wiki/tree/eafcc77001e496cc43499e4923b663aec722c813). It intentionally replaces path-based Markdown links with basename wikilinks and makes the three-file ingest transaction mandatory.
