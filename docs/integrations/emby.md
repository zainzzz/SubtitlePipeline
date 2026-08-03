# SubtitlePipeline × Emby 集成指南

> 适用场景: NAS 上自用部署 SubtitlePipeline + Emby,让字幕自动生成、自动被 Emby 识别。
>
> 阅读时间: 10 分钟
>
> 适用版本: Emby 4.8.x + SubtitlePipeline 当前 main

---

## 0. TL;DR(最快路径)

```bash
# 1. 拉本仓库
git clone https://github.com/yourname/SubtitlePipeline.git
cd SubtitlePipeline

# 2. 把媒体库放进 ./data(可改 docker-compose.emby.yml 的 volumes)
ln -s /volume1/media ./data   # 如果你已经用 Synology 媒体库

# 3. 一键起
docker compose -f docker-compose.emby.yml up -d

# 4. 打开浏览器
#    Emby:           http://localhost:8096   → 首次初始化(管理员账号、媒体库)
#    SubtitlePipeline: http://localhost:8000   → 模型管理 + 设置
#
# 5. 在 SubtitlePipeline 设置页 → 通知 & Webhook:
#      webhook_enabled = 启用
#      webhook_type    = emby
#      webhook_url     = http://emby:8096
#      webhook_token   = <见下文,Emby API key>
#      webhook_library_id = <见下文,你的 Emby library id>
```

走完这 5 步,新视频放进 `./data`,SubtitlePipeline 自动扫描 → 生成 `.srt` → 通过 webhook 通知 Emby 立刻刷新库。

---

## 1. 媒体库挂载约定

`docker-compose.emby.yml` 的关键约定:

| 容器内路径 | host 路径 | 用途 |
|---|---|---|
| `/data` | `./data` | 媒体库(Emby 和 SubtitlePipeline 都读;SubtitlePipeline 写 `.srt` 到这里) |
| `/config` | `./config` | SubtitlePipeline 数据库 + 配置 |
| `/emby-config` | `./emby-config` | Emby 配置 / 缓存 / 元数据库 |
| `/models` | `./models` | ASR 模型(只 SubtitlePipeline 用,会占 ~1-10GB) |
| `/output` | `./output` | 兜底输出目录(`file.output_to_source_dir=false` 时用) |

**关键点**: `/data` 是**共享卷**。Emby 在这里读视频,SubtitlePipeline 在这里写 `.srt`。两者必须看到同一个文件系统。

### 1.1 你需要把媒体库放哪?

- 已经在 NAS 上有媒体库(Synology / Unraid / 极空间等)?**软链** 进 `./data`:
  ```bash
  ln -s /volume1/media ./data
  ```
  或者直接改 compose 文件,把你的真实路径挂到容器内 `/data`。
- 没有现成库?把视频放进 `./data/movies`、`./data/shows` 等子目录。

### 1.2 文件权限问题(NAS 上最常踩的坑)

Emby 和 SubtitlePipeline 在容器内默认跑 UID/GID 1000。如果你的媒体库 owner 是 `admin:users` (uid 1026),会出现:

- Emby 看不到视频
- SubtitlePipeline 写不进去 `.srt`

**两种解决方案**:
1. **统一 UID**:compose 顶部用 env 改:
   ```bash
   UID=1026 GID=100 docker compose -f docker-compose.emby.yml up -d
   ```
2. **把媒体库 chown 到 1000:1000**(更彻底,影响范围大):
   ```bash
   sudo chown -R 1000:1000 /volume1/media
   ```

---

## 2. Emby API key(给 SubtitlePipeline webhook 用)

1. 浏览器打开 Emby `http://localhost:8096`
2. 右上角 → 齿轮图标 → **高级** → **API 密钥**
3. 点 **+ 新 API 密钥**
4. 应用名随便填(比如 `subtitle-pipeline`),**不要**勾选任何权限限制
5. 点 **确定**,**复制 token**(一长串十六进制字符串)

把这个 token 粘到 SubtitlePipeline 设置页的 `webhook_token`。

---

## 3. Emby Library ID(可选,提升 webhook 效率)

如果你只在某些库里要刷新,而不是整个媒体库:

1. Emby Web UI → **设置** → **库** → 点选你要的库(电影 / 电视剧 / 音乐...)
2. 看浏览器地址栏,URL 末尾的数字就是 library id,比如:
   ```
   /web/settings/library.html?itemId=42
                              ↑
                          library id = 42
   ```
3. 多个库用英文逗号分隔,例如 `42,43`

**留空也行**:Webhook 会调 Emby 的 `Library/Refresh` 端点,刷新全部库。多花几秒而已,日常用没差别。

---

## 4. SubtitlePipeline 这边的配置

打开 `http://localhost:8000`,第一次会走 SetupWizard。关键设置:

### 4.1 媒体库路径
- **input_dir**: `/data`(容器内路径,不是 host 路径)
- **output_to_source_dir**: ✅ 打开(字幕写到视频同目录,Emby 自动识别)
- **scan_interval_seconds**: `5`(默认)

### 4.2 通知 & Webhook(关键)
- **启用**: ✅
- **服务器类型**: `Emby`
- **服务器地址**: `http://emby:8096`
  - **注意**: 这里用 Docker 内部域名 `emby`,不是 `localhost`。两个容器在同一 compose 网络内,服务名 `emby` 会被 DNS 解析。
  - 如果你只用 SubtitlePipeline 不跑 Emby(反向连接到别处的 Emby),这里填 Emby 的实际地址比如 `http://192.168.1.10:8096`
- **API Token**: 上面第 2 节拿到的 token
- **媒体库 ID**: 上面第 3 节的 ID,逗号分隔,留空扫全部
- **编辑字幕时也触发**: ✅ 打开(用 #2 字幕编辑 webhook 的特性)
- **编辑防抖窗口**: `5`(秒)

### 4.3 ASR 模型
- 第一次进模型管理 → 下载一个小的(比如 `whisperx-small`)先跑通流程
- 跑通了再换大模型

### 4.4 翻译
- 如果用 OpenAI / Anthropic / 国产 LLM,填好 `api_base_url` / `api_key` / `model`
- **temperature** 默认 0.3 偏稳;想要更"信达雅"可以试 0.5
- 字幕类型选 **电影/动漫/纪录片** 等,prompt 预设会跟着变

---

## 5. 端到端验证

### 5.1 跑通一条
1. 放一个短视频(3-5 分钟,清晰对话)进 `./data/test/`
2. 等 5-30 秒(扫描间隔 + 任务入队)
3. SubtitlePipeline 首页应该看到新任务,status 变 `done` 后,`./data/test/` 下有 `<视频名>.forced.zh.srt` 这种文件
4. Emby 首页 → 找到这个视频 → 点开 → 字幕轨下拉,应能看到 "Chinese (forced)"

### 5.2 验证 webhook
- SubtitlePipeline 任务详情页 → 字幕卡片 → 看到 "已通知 emby ✓" 徽章
- 没有徽章?打开 worker 日志:
  ```bash
  docker compose -f docker-compose.emby.yml logs worker | grep -i webhook
  ```
  看到 `Webhook (emby) sent for task N` 就 OK;看到 `failed` 就对照第 6 节排查。

---

## 6. 故障排查(按频率排)

### 6.1 Emby 看不到字幕
- **路径对吗**:SubtitlePipeline 写 `<stem>.forced.zh.srt` 进视频同目录,不是单独的 `subtitles/` 子目录。
- **文件名格式**:在 SubtitlePipeline 设置 → 字幕输出 → 文件名模板,默认是 `{stem}.forced.{lang}.srt`。`{lang}` 会替换成 `zh` / `en` / `ja` 等。
- **字幕优先级**:Emby 默认选第一个匹配的字幕轨。如果视频自带英文字幕,你生成的会被标记 `forced` 但优先级可能低。**Emby 控制台 → 用户 → 字幕 → "显示 forced 字幕"** 打开。

### 6.2 Webhook 401 Unauthorized
- API key 复制错了?回第 2 节重新生成。
- API key 被 Emby 删除/重置?重新生成 + 更新 SubtitlePipeline 配置 + 保存。

### 6.3 Webhook 404 Not Found
- `webhook_url` 写错了。容器间用 `http://emby:8096`,不是 `http://localhost:8096`。
- Emby 还没起来就保存了配置?改完 webhook_url 等几秒重试。

### 6.4 Worker 跑得很慢 / OOM
- ASR 模型太大?切到 `whisperx-small` 或 `faster-whisper-small`。
- 视频太大?考虑在字幕设置加 `min_size_mb` / `max_size_mb` 过滤范围。
- NAS 内存紧张?在 compose 里加 `mem_limit: 4g` 给 subpipeline。

### 6.5 Emby 库不刷新
- `webhook_library_id` 写错了?留空试试,改成扫全部库。
- Emby 媒体库路径不是 `/data`?Emby 控制台 → 库 → 编辑 → 路径里**不能**有子目录大小写不一致(Linux 区分大小写,Windows 不区分)。

### 6.6 字幕能跑但翻译质量差
- 设置 → 翻译 → **LLM 采样参数** → `temperature` 试 0.5(更灵活)或 0.1(更稳)。
- 切换 **内容类型**:电影 / 动漫 / 纪录片 / 技术演讲 等预设,prompt 会跟着切。
- 觉得翻译不流畅?`frequency_penalty` 降到 0.5 试试。

---

## 7. 升级 / 维护

```bash
# 拉新代码
git pull

# 重建 SubtitlePipeline 镜像 + 重启(Emby 容器不动)
docker compose -f docker-compose.emby.yml build subpipeline
docker compose -f docker-compose.emby.yml up -d subpipeline
```

Emby 容器不重建,媒体库元数据无损。

---

## 8. 反向:用别处的 Emby(单 SubPipeline 容器)

如果你已经有 Emby 在另一台机器上跑(比如 `192.168.1.10`),只想跑 SubPipeline:

```yaml
# docker-compose.yml(默认那个就行)
services:
  subpipeline:
    image: saaak/subtitlepipeline:latest
    # ...
    environment:
      # ...
    volumes:
      - ./data:/data            # 你的媒体库本地路径
      - ./config:/config
      - ./models:/models
```

然后 webhook_url 填 `http://192.168.1.10:8096`,token 是那台 Emby 的 API key。

---

## 9. 进阶:Jellyfin / Plex

Jellyfin 几乎和 Emby 一样,只是 webhook_type 选 `jellyfin`,Emby API key 替换成 Jellyfin 的(在 Jellyfin 控制台 → API Keys 创建)。

Plex 选 `plex`,token 填 Plex Token(`?X-Plex-Token=xxx` 那个),library id 填 section id(Plex 媒体库编辑页 URL 末尾数字)。
