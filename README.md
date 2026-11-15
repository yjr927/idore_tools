# idore · TikTok 视频发现与创作工作台

包含本地创作工作台，以及独立的 GitHub 云端 TikTok 公开视频采集试跑程序。

## ChatGPT Work 云端采集

推荐先在网页版 Work 使用已登录的云端浏览器采集，再通过 GitHub 插件更新 `work-results/latest.json`。网站发布时验证并合并这些观察，展示原片是否观看、动作检查和同玩法证据。新采集使用 v2：每个纽约自然日收录 1–2 条不同原视频，K-pop 最多 1 条、虚拟偶像最多 1 条；质量不足允许 0 条；同视频当天重试不重复占名额，往日视频复查计入当天名额。男女素材、单人／多人是独立分类，不增加每日总名额。旧版 v1 记录继续保留。操作范围与固定格式见 [Work 接入说明](docs/CHATGPT_WORK.md)。

这条通道不需要把 ChatGPT 登录信息或 API 密钥放到网站里，也不需要 Browserbase。本地代码或说明更新不代表线上网站已发布，也不代表 Work 每日计划已更新或启用。已有任务需在原 Work 聊天中更新指令并检查保存状态；首次建立任务前，先完成写入与发布验证。登录过期或人工验证仍需用户介入。

**匿名云端试跑验证公开页面可读性；Work 通道另行记录实际播放与观察证据。发现链接不等于已确认热门、单人动作或 AI 可复刻。** 页面要求登录、验证码或不返回列表时，程序停止并保存真实状态，不用示例视频填充。

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

GitHub Pages 只发布 `site/`，不会发布本地 Python 服务和角色素材。它展示采集结果、来源、最后尝试／成功时间，支持按 K-pop／虚拟偶像及动作类型筛选，也可独立组合男／女素材与单人／多人筛选，另有浏览器本地收藏和原视频入口。候选按首次抓取的纽约日期倒序排列，同日分男生／女生素材，并保留混合与待分类区；复查旧视频不会把它变成新素材。所有候选和历史快照均展示男／女素材、单人／多人／待确认以及变妆、换装或无变装的观察标记。`material_group=mixed` 表示同时需要男生和女生素材，人数使用原有 `checks.solo`；二者不混用。只有看原片确认 1 名可见人物／角色才标单人，实际至少 2 名才标多人，无人物或不确定时保留待确认。旧版缺失信息不会自动猜测补齐，需另行回看和重分类；当前每日采集不承担全库历史回填，也不代表旧素材已全部分好。刷新按钮仅重新读取已发布的数据，不会启动后台采集。

“采集记录”按纽约时间展示最近 90 次尝试，展开可看当次采到的具体视频、主题、男／女素材分组、单人／多人观察、变装类型、玩法、动作观察、原片链接和当时的指标。每轮保存独立快照，后续更新不会改写旧记录；没有结果或访问失败也会记录。

## 可选 OpenAI 分析

直接读取公开页面不需要 OpenAI。只有手动运行任务时选择 `analyze=true` 才会调用模型，并可能产生 API 费用。

1. **Settings → Secrets and variables → Actions → Secrets**：新增 `OPENAI_API_KEY`。
2. 同一页面 **Variables**：新增 `OPENAI_MODEL`，填写你账号中可用、支持结构化输出的模型 ID。
3. 手动运行采集，勾选 `analyze`。

这项采集分析仅根据文字元数据提供玩法分类建议；造型图、成片文案与 hashtags 使用下方独立创作入口，不会随采集自动付费调用。没有观看视频时，不会标记人数、动作或热度已核验。没有密钥／模型配置时明确显示未配置，不会伪装调用成功。密钥不写入源码、浏览器、结果或日志。

## 真实视频生成入口

已提供独立的 [OpenRouter Seedance 连接与生成说明](docs/INTEGRATION.md)。本机启动后打开 `http://127.0.0.1:8765/generate`，在密码框连接 OpenRouter；密钥只保留在服务内存中。模型与参数读取实时列表，支持文字、首尾帧和角色／动作参考三种方式，明确确认后才提交付费生成，完成后保存、播放与下载 MP4。旧的分镜计划仍为模拟，不会因批准计划而调用生成。

也可把 **OPENROUTER_API_KEY** 保存到 GitHub Actions Secrets，再手动运行 **Generate Seedance video**。密钥不进入公开网站；结果以任务附件保存，不自动发布。公开仓库的工作流输入可见，私人素材请用本机入口。造型图、英文文案与 hashtags 也已接入，使用单独的 OPENAI_API_KEY；TikTok 发帖仍需人工完成。

## 造型图 → 视频 → 英文文案与 hashtags

同一个 `/generate` 页面可分别连接 OpenAI 与 OpenRouter，两个密钥仅在服务内存中使用。先选择角色参考图及前后造型，调用 OpenAI 生成一对图片；再一键填入 Seedance 首尾帧，检查后提交视频。成片完成可在本机抽取三张关键帧，交给 OpenAI 生成 3 版英文文案和 3–5 个标签。联网趋势选项会额外检索美国近期资料，保留真实来源与检索时间；缺乏来源时只给内容／品牌建议，不称实时热榜。

GitHub 云端需要两个 Repository Secrets：**OPENROUTER_API_KEY**、**OPENAI_API_KEY**。`creative-generation.yml` 提供造型图、文案和完整视频链路的手动任务；完整链路还需在 Variables 配置文案模型 **OPENAI_MODEL**，图像与 Seedance 模型在任务表单中明确选择。生成都需勾选本次额度授权，不随每日采集、代码推送或刷新自动启动。

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
