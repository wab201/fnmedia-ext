# 飞牛影视 · 在线资源扩展 (fnmedia-ext)

在飞牛影视**原生搜索**里聚合苹果 CMS 资源站的在线影视，点开即用飞牛自带播放器**直接播放**。在线片库更齐全，搜索结果默认**在线前排**。零侵入、完全可逆：不改官方二进制 / nginx / 数据库，靠 Unix Socket 原子接管。

> 点播源会随时间失效，因此本扩展**不做转存/STRM 落库**——只做即搜即播；源不可用时可在管理页删除或替换。

## 适配版本

| 项目 | 已验证 / 要求 |
|---|---|
| fnOS | **1.2.0 及以上**（FPK manifest 最低要求） |
| 官方影视应用 | **trim.media `0.9.8-1`**（已实测） |
| CPU 架构 | `all`（纯 Python，安装时创建独立 venv） |
| 安装方式 | fnOS 应用中心手动安装 FPK，可安装在 `/vol1`、`/vol2` 等数据卷 |
| 依赖 | 官方「影视 (trim.media)」必须保持安装并启用 |
| 网络 | 资源站访问使用直连（`trust_env=False`），不读取代理环境变量 |

> 本扩展通过 Unix Socket 可逆接管官方影视接口，不改官方二进制、nginx 或数据库。未来若 fnOS / trim.media 升级后签名常量或接口结构变化，停用扩展会立即恢复官方服务，等待新版本适配。

## 下载安装

1. 打开 [Releases](https://github.com/yujian0815/fnmedia-ext/releases)，下载最新的 `fnmedia-ext-<version>.fpk`。
2. 在 fnOS **应用中心 → 手动安装 FPK** 中选择该文件。
3. 选择应用安装所在存储卷，保持依赖应用 **影视 (trim.media)** 为运行状态。
4. 安装完成后启用「飞牛影视在线扩展」，桌面/应用内会出现 **影视在线扩展管理**。
5. 打开官方「影视」，直接搜索中文片名即可看到带 `【在线】` 标记的结果。

管理入口为应用内 iframe：`/app/fnmedia-ext/`；旧的直连路径 `/v/online-admin` 仍保留。卸载或停用扩展会自动把 `/var/run/trim_media.sock` 还原给官方影视服务。

## 工作原理

```
浏览器/飞牛App → nginx(/v) → /var/run/trim_media.sock → fnmedia-ext(FastAPI)
                                                   ├─ 普通请求 → 透传官方后端(upstream socket)
                                                   ├─ /search/list → 注入在线结果（默认排最前）+ 本地结果
                                                   ├─ /item /season/* /episode/* /play/* /stream/* → 伪造链路
                                                   ├─ mediadb/list · mediadb/sum · item/list · item/<guid>
                                                   │      → 注入「在线电影/剧集/动漫/综艺」分类虚拟媒体库
                                                   └─ /v/media/online/* → HLS/分片/密钥/mp4 反向代理
```

### 在线热门分类媒体库（方案 A · 深度爬取）

- 后台定时（默认每 6 小时）**深度爬取**每个资源站「最近更新」多页（默认 6 页/源、每源最多 120 条），
  多源跨片名+年份聚合去重，默认保留约 300 条，原子写入本地缓存 `state/hot.json`；整轮约 10 秒。
- 首页/媒体库请求只读缓存，列表接口 2-5ms 返回，**绝不被网络请求阻塞**；进入媒体库时若缓存已超过
  刷新间隔，会在后台自动补刷一次（stale-while-revalidate，本次显示旧内容、下次进入即最新），也可管理页「立即刷新」。
- 在媒体库列表最前面插入 4 个虚拟库：**🌐在线电影 / 🌐在线剧集 / 🌐在线动漫 / 🌐在线综艺**，
  按资源站 `type_name`（剧情片/动作片/日剧/韩剧/动漫/综艺…）自动归类；卡片复用在线播放链路
  （详情/选集/线路/续播）。旧版单库 guid 仍可打开，自动映射到「在线剧集」。
- 容错：任一线路失败不影响其它线路；**全部线路失败或结果异常为空时保留旧缓存**，并在管理页显示 `last_error`；
  海报加载失败时返回内联 SVG 占位图（HTTP 200），不会出现裂图。
- 完整兼容官方前端的 `page/page_size` 无限滚动分页、`tags.type`（电影/电视剧）筛选、
  `is_favorite`、`parent_guid` 以及 `sort_column`（`create_time` / `sort_title` / `release_date` / `vote_average`）。
- 可在管理页开关、调整总条数上限(60-800) / 每源条数(20-240) / 每源页数(1-10) / 刷新间隔(1-24h)，并可「立即刷新」。

- `supervise.py` 先校验官方 socket 的监听者 exe，再在私有暂存 socket 启动本服务；
  健康检查通过后用 `renameat2(NOREPLACE)` 原子换名接管；退出/崩溃时原子换回官方 socket（fail-closed）。
- 播放：HLS 源返回自研 `play_link=/v/media/online/<media_guid>/preset.m3u8`，
  服务端实时抓取源站 m3u8，把分片/密钥 URI 重写为同源代理地址（透传 Referer/UA/Range）；
  mp4 等非 HLS 直链返回 `/direct`，流式反向代理并透传 `Range`（支持拖动进度，206 直通）。
- 在线条目 guid = `md5("on_<provider>_<vodid>")`，media/video/season/episode guid 确定性派生、可反查。
- 标题与线路持久化在 `state/titles.json`，重启后历史卡片仍可播放。

## 目录

```
proxy/
  app.py            主代理：透传 + 搜索注入 + 播放伪造 + HLS/海报代理 + 管理 API
  supervise.py      原子接管守护（暂存启动→校验→换名；退出自动回滚）
  providers.py      苹果CMS V10 适配 + 连通性探测 + /share 播放页解析（直连国内，trust_env=False）
  inject.py         在线条目 → 飞牛 item/stream/play/history 结构（标题带【在线】）
  store.py          标题/线路持久化、跨源聚合、按集解析、线路/集反查索引
  hot.py            「在线热门」后台刷新：跨源聚合去重、排序、原子缓存、失败保留旧缓存
  history.py        在线播放进度（继续观看）：record 落库、play/list 合并、ts 续播
  config.py         可热加载配置：自定义源增删、启用开关、噪音过滤、在线前排
  admin_ui.py       单文件管理页（源管理/测速/设置）
  fnmedia_authx.py  官方 authx 签名客户端（MD5 + apiKey）
deploy/             持久 systemd 单元 + install/uninstall
packaging/fpk/      飞牛应用中心 FPK 打包
state/              titles.json / config.json（运行时生成）
```

## 从源码构建 FPK

普通用户建议直接下载 Release 的 `.fpk`。开发者可从源码构建：

```bash
git clone https://github.com/yujian0815/fnmedia-ext.git
cd fnmedia-ext
bash packaging/fpk/build.sh
# 产物：dist/fnmedia-ext-<version>.fpk
```

FPK 安装回调会自动在应用目录创建独立 venv，并安装 `requirements.txt` 中声明的运行依赖。

如需手工在开发机直装（不推荐用于生产 NAS）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo bash deploy/install.sh
```

还原 / 卸载：`sudo bash deploy/uninstall.sh`（加 `--purge` 连运行配置 `state/` 一起删，不影响官方数据）。

## 使用

- 打开飞牛影视，搜索任意**中文片名**（拼音上屏后才触发在线搜索，纯拼音碎片只走本地）。
- 标题带 **【在线】** 前缀、系列列显示「🌐在线·年份·地区」的卡片，点开即播；在线结果默认排在最前。
- 管理页（同源）：`https://<你的飞牛地址>/v/online-admin`
  - **资源站管理**：启用/停用、添加自定义苹果 CMS JSON 接口、删除（内置源可一键恢复）
  - **连通性测试**：单源测试或「全部测速」，显示耗时、命中条数、直链数，方便筛掉失效源
  - 每词注入条数、在线前排开关
  - **噪音关键词自定义**：标签式增删、批量粘贴（逗号/顿号/换行分隔）、一键恢复默认；
    标题或备注命中任一关键词即不注入
  - **在线热门分类媒体库**：开关，总条数上限 / 每源条数 / 刷新间隔，查看最近刷新时间、
    成功线路数与错误信息，可「立即刷新」；自动分为在线电影 / 剧集 / 动漫 / 综艺四库

## 资源站接口格式

添加的自定义源需为苹果 CMS V10 标准 JSON 接口，形如：

```
https://站点/api.php/provide/vod/at/json        # 搜索 ?ac=detail&wd=关键词
```

返回 `{"code":1,"list":[{vod_name, vod_play_url, ...}]}` 即被识别。

## 管理 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v/api/ext/status` | 运行状态 + 配置（不含 token）|
| GET | `/v/api/ext/online?q=&limit=` | 已缓存在线条目 |
| POST | `/v/api/ext/config` | 保存设置/源启用状态（热生效，清搜索缓存）|
| POST | `/v/api/ext/junk-keywords` | `{keywords}` 自定义噪音词（自动分隔去重）或 `{reset:true}` 恢复默认 |
| POST | `/v/api/ext/source/add` | `{name,api}` 添加自定义源（按地址去重）|
| POST | `/v/api/ext/source/delete` | `{id}` 删除源（内置源仅隐藏，可恢复）|
| POST | `/v/api/ext/source/restore` | 恢复全部内置源 |
| POST | `/v/api/ext/source/test` | `{id}` 或 `{api}` 单源连通性探测 |
| POST | `/v/api/ext/source/test-all` | 并发探测全部源 |
| POST | `/v/api/ext/hot/refresh` | 立即刷新「在线热门」缓存（后台聚合，失败保留旧缓存）|
| GET | `/v/online-admin` | 管理页 |

## 选集（原生 TV UI）

- 单集条目渲染为 **Movie**（点开即播）；多集条目渲染为 **TV**。
- TV 走飞牛原生选集接口：`GET /season/list/:tvGuid` 返回一个伪造 Season，
  `GET /episode/list/:seasonGuid` 返回每一集（确定性 `episode_guid=md5(ep:on:idx)`，
  集身份与播放线路解耦，切线路不换集）。
- 点某集 → `play/info{item_guid:episodeGuid}` → `play/play`，media/video guid 与该集绑定，
  播放器拉到的就是该集 HLS。媒体 guid 反查索引在 store 内存中维护，随搜索结果增量重建。

## 多线路切换

- 搜索时把**不同资源站命中的同一部片聚合成一个条目**（片名规范化 + 年份归并），
  每个资源站各成一条播放线路（线路键 `provider::组名`，同一站直链优先于 share 页，
  直链最多的线路为默认）。
- 播放设置里的**清晰度菜单即「线路」菜单**：`POST /stream` 与 `GET /stream/list`
  返回的 `qualities` 为每个资源站生成一项（暴风、非凡、量子……）。播放器切换该项时
  会重新调用 `POST /play/play`，代理用请求里的线路码（编码在 `bitrate` 字段）解析
  对应源，返回只含该线路的播放列表，从而在卡顿/失效时即时换源。
- 标题播放键对 TV 也可直接点击（`can_play=1`）：从未看过播第 1 集；有未看完的集则
  续播该集并恢复到上次秒数；已看完的集之后自动播下一集。
- 嵌套 master（源 `index.m3u8` 再套一层子 m3u8）会自动跟随解析（最多 4 层），
  并正确重写 `EXT-X-KEY`（AES）与 `EXT-X-MAP`（fMP4 初始化段）。
- `/share/<id>` 等播放页源在**播放时按需解析**（页面内嵌 `const url=...m3u8` 提取，
  30 分钟缓存），不拖慢搜索；直链线路始终优先于仅可解析的线路。

## 播放记录 / 继续观看

- 在线播放进度（`POST /play/record`，播放器高频上报）落在本地 `state/history.json`，
  不上报官方库。
- 代理 `GET /play/list` 时把在线历史（带海报、剧名、季集、`ts` 进度、`duration`）
  与官方本地历史合并返回，因此在线条目出现在「继续观看」，置顶按最近播放排序；
  进度 ≥95% 视为看完不再占位，`DELETE /play/record` 同步删除。
- `POST /play/info` 回填已记录的 `ts`（秒），播放器据此从上次位置续播。

## 打成 FPK 应用包

```
bash packaging/fpk/build.sh          # 产出 dist/fnmedia-ext-<ver>.fpk
```

- `deploy/fnmedia-ext.service.in` 是 systemd 模板，安装时 `deploy/install.sh` 按
  实际落盘路径渲染绝对路径，因此同一包可装到任意目录。
- `cmd/install_callback` 在首次安装时创建 venv 并 `pip install fastapi uvicorn httpx`，
  随后调用 `deploy/install.sh` 接管；`uninstall_callback` 调用 `deploy/uninstall.sh` 还原。

## 已知限制 / 路线图

- `/share/<id>` 解析器目前覆盖内嵌 `const/var url` / `{"url":...}` / 明文 m3u8
  的播放页（苹果系常见）；个别用加密 JS 动态拉流的页面可能解析不到（自动跳过该线路）。
- 多线路聚合依赖「片名规范化 + 年份」判定，极少数同名不同作品理论上可能被并为一条。
- 已在 fnOS 应用中心完成全新安装 / 停用 / 启用验证；适配版本以本文顶部「适配版本」表为准。
  官方 trim.media 升级若改动签名常量或接口结构，需要相应更新本扩展；停用即可恢复官方服务。
