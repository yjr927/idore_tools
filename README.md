# idore · TikTok 视频发现与创作工作台

包含本地创作工作台，以及独立的 GitHub 云端 TikTok 公开视频采集试跑程序。

## ChatGPT Work 云端采集

推荐先在网页版 Work 使用已登录的云端浏览器采集，再通过 GitHub 插件更新 `work-results/latest.json`。网站发布时验证并合并这些观察，展示原片是否观看、动作检查和同玩法证据。操作范围与固定格式见 [Work 接入说明](docs/CHATGPT_WORK.md)。

这条通道不需要把 ChatGPT 登录信息或 API 密钥放到网站里，也不需要 Browserbase。通道准备完成并不代表每日计划已经启用；首次写入与发布验证后，才能在对应的 Work 聊天中设置每日任务。登录过期或人工验证仍需用户介入。

**当前云端功能是验证公开页面可读性。发现链接不等于已确认热门、单人动作或 AI 可复刻。** 页面要求登录、验证码或不返回列表时，程序停止并保存真实状态，不用示例视频填充。

## GitHub 云端试跑

打开 [TikTok public discovery](https://github.com/yjr927/idore_tools/actions/workflows/tiktok-discovery.yml)，选择 **Run workflow**。

- 使用 GitHub 托管的 Chromium，直接读取 TikTok 公开搜索页。
- 默认寻找简单单人手势／舞蹈、变装、表情／对口型，最多 3 次搜索、8 条链接。
- 不使用外部趋势文章、付费采集 API、代理、账号密码或登录 cookie；不处理验证码。
- 保存去重后的候选、首次发现时间、最后采集时间和每个来源的读取状态。
- 数据在 `site/data/discovery.json`，每次运行另有可下载的结果文件。
- 此匿名试跑保留为手动运行；每日采集改由已登录的 Work 云端任务执行，是否启用以 Work 中的计划记录为准。
- 采集受阻时保留旧候选，并把任务标为未取得有效结果；不能把工作流绿色状态当作视频已验证。

## 趋势结果看板

`site/` 是不含密钥的静态看板。要启用线上网址：仓库 **Settings → Pages → Build and deployment → Source → GitHub Actions**，然后运行 **Publish trend viewer**。

GitHub Pages 只发布 `site/`，不会发布本地 Python 服务和角色素材。它展示采集结果、来源、最后尝试／成功时间，支持筛选、浏览器本地收藏和打开原视频。刷新按钮仅重新读取已发布的数据，不会启动后台采集。

“采集记录”按纽约时间展示最近 90 次尝试，展开可看当次采到的具体视频、玩法、动作观察、原片链接和当时的指标。每轮保存独立快照，后续更新不会改写旧记录；没有结果或访问失败也会记录。

## 可选 OpenAI 分析

直接读取公开页面不需要 OpenAI。只有手动运行任务时选择 `analyze=true` 才会调用模型，并可能产生 API 费用。

1. **Settings → Secrets and variables → Actions → Secrets**：新增 `OPENAI_API_KEY`。
2. 同一页面 **Variables**：新增 `OPENAI_MODEL`，填写你账号中可用、支持结构化输出的模型 ID。
3. 手动运行采集，勾选 `analyze`。

模型只根据已获得的文字元数据提供玩法分类建议。没有观看视频时，不会标记人数、动作或热度已核验。没有密钥／模型配置时明确显示未配置，不会伪装调用成功。密钥不写入源码、浏览器、结果或日志。

## 本机工作台

需要 Python 3.10+。双击 `start.command`，或运行：

```sh
python3 server.py --open
```

浏览器打开 `http://localhost:8765`。完整本地功能说明见 [本地工作台说明](docs/LOCAL_WORKSPACE.md)。

`data/`、`library/`、角色图片、ZIP、预览截图、个人环境配置均不上传本仓库。你的本机仍保留原有素材；从 GitHub 新下载的项目不会包含这些私人素材。公开云端结果与本机素材库分别存储。

## 验证

```sh
python3 -m unittest discover -s tests -v
```

云端采集依赖单独列在 `requirements-cloud.txt`。本机常规工作台不需要 Playwright。
