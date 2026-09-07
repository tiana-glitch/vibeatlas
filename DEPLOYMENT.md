# VibeAtlas 公开演示部署

## 发布边界

本仓库包含本地工具代码和一个可公开复用的 `ai-pm-knowledge-vault` skill。
公开部署只允许使用 `demo_vault/` 中的合成数据；不要上传个人根目录中的
`.note_runs/`、`.note_runtime/`、`.obsidian/`、真实 `07-素材附件/`、真实
`08-知识点/` 或工作笔记。`.agents/` 下只纳入本仓库的脱敏 skill，不纳入其他
本机 Agent 配置。

`demo_vault/` 不包含真实个人、公司、客户、截图或附件。它只是 API 和知识点
图谱的可重复演示数据。

## Render

1. 将本项目推送到 GitHub 的公开仓库。
2. 在 Render 选择 **New + -> Blueprint**，指向仓库中的 `render.yaml`。
3. 确认服务使用 Python runtime，并等待健康检查 `/api/health` 返回 `200`。
4. 使用 Render 分配的 `https://<service>.onrender.com/graph.html` 验证图谱，
   `/` 验证笔记整理台。

`render.yaml` 使用以下命令启动服务：

```bash
python3 server.py --host 0.0.0.0 --port $PORT --vault-root demo_vault
```

## Vercel（Docker 容器演示）

仓库根目录的 `Dockerfile.vercel` 是 Vercel 容器函数的入口。它与现有的
`Dockerfile` 分开维护：Vercel 默认监听 80 端口，并只把 `demo_vault/` 作为公开
演示数据源，不会把本机 Obsidian 知识库放进镜像。

1. 在 Vercel 选择 **Add New -> Project**，导入
   `tiana-glitch/vibeatlas`，保持项目根目录为仓库根目录。
2. 如果出现框架选择，选择 **Other**；不要填写额外的 Build Command 或
   Output Directory，让 Vercel 自动识别 `Dockerfile.vercel`。
3. 点击 **Deploy**，等待容器构建完成。部署详情中的 `*.vercel.app` 地址就是
   公网入口。
4. 用以下地址做最短验收（把 `<deployment>` 换成实际域名）：

   ```text
   https://<deployment>.vercel.app/
   https://<deployment>.vercel.app/graph.html
   https://<deployment>.vercel.app/inbox.html
   https://<deployment>.vercel.app/api/health
   ```

当前公开演示地址：
`https://vibeatlas-eosin.vercel.app`

对应入口：

- 工作台：`https://vibeatlas-eosin.vercel.app/`
- 知识图谱：`https://vibeatlas-eosin.vercel.app/graph.html`
- 收件箱：`https://vibeatlas-eosin.vercel.app/inbox.html`
- 健康检查：`https://vibeatlas-eosin.vercel.app/api/health`

如果要绑定自己的域名，在 **Project -> Settings -> Domains** 添加域名，并按
Vercel 页面显示的 A/CNAME 记录修改阿里云 DNS；不要凭记忆填写记录值。域名解析
生效后，以上路径会自动使用自定义域名。Vercel Hobby 计划通常可用于小流量演示，
但是否需要额外账号验证、额度和容器功能以当前控制台提示为准。

### Vercel 运行边界

- 容器函数是无状态的。公开演示中的收录、归档和 OCR 修订写入临时实例磁盘，
  重启、扩容或重新部署后可能消失；不要把它当作真实知识库。需要持久化时应接入
  Vercel Blob、KV 或数据库，并把写入接口加上登录和权限控制。
- 当前 API 没有鉴权、限流或多用户隔离，任何拿到地址的人都能调用写入接口；只放
  合成数据，不要上传个人笔记、公司资料或密钥。
- Vercel Functions 的请求/响应体上限约为 4.5 MB。页面当前按本地服务允许 20 MB
  文件，但公网容器收到较大的 Base64 请求会在到达应用前返回 413；线上先使用小于
  约 3 MB 的文档/图片，后续再改用对象存储直传。
- Vercel 容器运行在 Linux，不能使用 macOS Vision OCR。未设置
  `WENXIN_API_URL` 与 `WENXIN_API_KEY` 时，图片 OCR 可能不可用；可改用文字回退
  输入，或在 Vercel 项目环境变量中配置受控的 OCR 服务（密钥只放服务端）。
- 关键词检索会请求 Bing RSS、DuckDuckGo 或 Wikipedia；外部网络波动时，流程会
  返回“无可用来源”，不会伪造引用。

## Hugging Face Spaces（免信用卡备选）

本仓库同时包含 `Dockerfile`，可作为公开 Docker Space 的构建入口：

1. 在 Hugging Face 创建一个 **Docker Space**，可见性选择 Public。
2. 将本仓库内容上传到 Space，或按 Hugging Face 提供的 Git 地址推送。
3. Space 会监听 7860 端口；启动命令自动使用 `demo_vault` 合成知识库。
4. 等待构建完成后，用 `https://<用户名>-<space名>.hf.space/` 验证工作台，
   `/graph.html` 验证知识图谱，`/api/health` 验证服务健康状态。

免费硬件通常无需信用卡，但平台可能要求单独的账号验证。公开 Space 只用于面试演示：
当前 API 没有登录、鉴权或限流，写入内容保存在临时磁盘，重启后可能丢失；不要放入真实
个人知识库、附件或任何 API 密钥。

`--vault-root` 将 API 的知识库读写根目录与静态页面目录分离。静态页面仍从
仓库根目录加载，API 只读取/写入 `demo_vault/`；demo vault 目录也不会被静态
文件服务直接暴露。

## 重要限制

- 当前服务适合面试演示，不是多用户生产服务：没有登录、鉴权、限流或审计账号。
- API 仍包含写入接口；不要把真实数据或 API 凭证放入公开服务。
- Render 免费实例可能休眠，且本地磁盘不保证跨部署持久化。演示数据应从 Git
  仓库重新生成；真实知识库需要独立的持久化存储和访问控制。
- 文心 OCR 只有在服务环境中显式配置 `WENXIN_API_URL` 与
  `WENXIN_API_KEY` 时才启用。不要把密钥写入仓库或前端。
- GitHub Pages 只能托管静态展示页，不能运行本项目 Python API。

## 发布前检查

```bash
git status --short
git diff --cached --name-only
python3 -m unittest discover -s tests -q
python3 .agents/skills/ai-pm-knowledge-vault/scripts/validate_vault.py demo_vault
```

确认暂存清单不含个人笔记、原始附件、运行记录或密钥后，再推送到远程仓库。

## GitHub Pages（可选）

GitHub Pages 适合只展示项目介绍页：
`https://<用户名>.github.io/<仓库名>/vibecoding-project.html`。
它不会提供 Python API；需要完整交互工作台时，请使用 Render 地址。
