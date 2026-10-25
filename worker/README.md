# Pica Cloudflare 分发引擎 (Worker)

本目录包含仓库的 **Cloudflare Worker** 代理逻辑。`wrangler.toml` 部署的是 `worker/index.js`；
`worker/worker.js` 是已被取代的历史副本，仅供对照，请勿部署。

## ⚡ 路由

| 路由 | 上游 | 缓存 |
|---|---|---|
| `/` | `r/index.json` | 60s |
| `/index.json`、`/r/index.json` | `r/index.json` | 60s |
| `/repo.json`、`/r/repo.json` | `r/repo.json` | 60s |
| `/<mgr>/index.json`、`/r/<mgr>/index.json` | 描述文件声明的路径 | 60s |
| `/latest/<file>` | `releases/download/latest/<file>` | 有 SHA256 → 7 天 immutable；无 → 300s 且不入缓存 |
| `/releases/<tag>/<file>` | `releases/download/<tag>/<file>` | 同上 |

含 `packages` 的索引（`/repo.json`、`/r/repo.json`、`/<mgr>/index.json`、`/r/<mgr>/index.json`）
在返回前会改写 `download_url`，见下节。入口描述文件（`/`、`/index.json`、`/r/index.json`）只有
`managers`，没有包记录，原样返回。

### 索引里的下载地址：Git 存上游，Worker 返回自己

Git 里的 `r/*/index.json` 保存的是 CI 写入的 GitHub Releases 绝对地址，Worker **不改这些文件**。
它只在返回索引响应时，把**自己确实能代理**的条目改写成同源地址：

| `download_url` 原值 | 返回给客户端 |
|---|---|
| `https://github.com/<本仓库>/releases/download/latest/<file>` | `https://<本次请求的 origin>/latest/<file>` |
| `https://github.com/<本仓库>/releases/download/<tag>/<file>` | `https://<本次请求的 origin>/releases/<tag>/<file>` |
| 其他主机、其他仓库、局域网地址 | 原样保留 |
| `filename` 与地址末段不一致 | 原样保留 |
| 文件名过不了 `/latest/` 路由的校验（含 `%`、非 `.pkg.tar.gz`、超 200 字符） | 原样保留 |
| 缺失 / 为空 | 原样保留，**不代为编造** |

三条规则的理由：

* **只改自己能为之负责的地址。** 改写后客户端会拿这个地址下载、并用条目里的 `sha256` 校验，
  所以只有"末段与 `filename` 逐字相同"才改——否则那份哈希描述的就不再是这些字节。
* **按 tag 发布的资产保留 tag。** 一律映射到 `/latest/` 会取到滚动发布里的另一份字节。
* **不代为编造地址。** 索引里没有 `download_url` 是上游的问题；凭空补一个会把坏索引藏在一个
  能用的下载后面。注意客户端对 `download_url: null` 的回退是 `<源根>/packages/<file>`，
  而本 Worker **没有** `/packages/` 路由，所以也不能靠置空来实现回退。

改写发生在响应边界，**不进 `readJson` 的缓存**：`serveAsset` 要回读缓存里的索引来决定上游
（见下节），缓存里若是改写后的同源地址，它就不再是 `github.com` 地址，所有资产都会退化成按
文件名反推 `latest/`，按 tag 发布的资产因此取错。

客户端行为：pica 读的是入口描述文件 + `/<mgr>/index.json`（`sync.rs` 里 `join_child(root,
"index.json")` 再按 `managers[].path` 取后端索引），**不读 `repo.json`**；`repo.json` 一并改写，
是为了同一个 origin 给出的两份索引不会在"去哪下载"上互相矛盾。

### 为什么后端索引要走描述文件

Worker 里没有包清单。`/apk/index.json` 这类请求会先读 `r/index.json`，只有当该路径
**被描述文件声明**时才向上游取；否则直接 404。没有这一步，这个路由就是一台开放的
`raw.githubusercontent.com` 读取器。

### 二进制为什么以 SHA256 做缓存键

文件名可以复用，字节不行。`repo.json` 里每个条目带 `sha256`，Worker 用它拼缓存键：
哈希不变就命中 Cloudflare 缓存，哈希一变就是新键，不会把旧字节发给拿到新索引的客户端。

上游地址优先取条目里的 `download_url` —— 让 Worker 跟着发布规则走，而不是自己从文件名
反推。但只跟 `https://github.com/<本仓库>/releases/` 下的地址：镜像若把 `download_url`
改写成自己，Worker 跟随就会打回自己，那是请求循环。

## 🚀 部署

* 基于 `wrangler.toml`（`main = "worker/index.js"`），`GITHUB_USER` / `GITHUB_REPO` 为环境变量。
* 测试：`node --test worker/test_worker.mjs`（缓存与上游均为内存桩，不联网）。

**地址**: `https://pica-pkg-repo.<username>.workers.dev/`

## ⚠️ 已知边界

* **下载从此依赖 Worker 可用。** 索引改写之后，经本 Worker 同步的客户端就从它下载二进制：
  Worker 不可用时，这些客户端能同步到索引却装不了包。这是"让 CDN 真正接管下载"要付的代价，
  不是能顺手消除的副作用。Git 里保留上游绝对地址，正是为了让配了别的源（GitHub raw、局域网
  镜像、`scripts/serve_mirror.py`）的客户端不受影响——`serve_mirror.py:104` 做的是同一件事。
* **改写只动 `download_url` 一个字段。** `sha256`、`size`、`csize` 一律原样返回，所以 Worker
  既不能、也不会纠正索引与已发布资产不一致的条目。
* **索引与已发布资产确实会不一致。** 同一次 CI 运行里索引先生成（`generated_at`）、资产后上传，
  相差几分钟属正常，这批实测相符（例：`bind-dig`）。问题出在**后一次运行只重新上传资产、没有重新
  提交索引**。2026-10-22 实测：`opkg` 索引 75 条里有 14 条资产的时间戳是 `10:25:24`，比索引的
  `09:35:55` 晚 47 分钟（`adguardhome`、`leigod-acc`、`rtp2httpd`、`rtp2httpd@jarod360`、`vane`）；
  其中 9 条连 `csize` 都与实际字节不符，其余长度相同但 `sha256` 不同。客户端会下载成功、然后以
  完整性校验失败拒绝安装。同期还有 1 条
  （`luci-theme-material3@KawaiiHachimi-26.156.15499~38397ed-1-all-opkg.pkg.tar.gz`）在 `latest`
  里根本不存在，Worker 与 GitHub 直连都是 404。这要在发布链上修——上传资产与提交索引必须出自
  同一次构建——Worker 侧改不了，改写 `download_url` 也修不了它。
