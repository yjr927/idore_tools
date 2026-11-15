# OpenRouter Seedance 接入与创作流程

已实现独立的真实视频生成入口，旧的分镜任务继续使用模拟进度。批准旧分镜不会自动花费额度或生成视频。

## 本机连接页

运行 `python3 server.py`，打开 `http://127.0.0.1:8765/generate`。在密码框输入 OpenRouter API Key，点击“验证并连接”。验证只读取账户密钥信息，不提交生成。密钥仅在本机服务内存中使用；不会保存到项目、浏览器存储、任务 JSON 或 GitHub，服务重启后需重新输入。断开连接会清除当前内存密钥。

页面读取 OpenRouter 当前视频模型列表，供用户明确选择 Seedance 版本、时长、分辨率、画幅和生成方式；不自动换用另一个付费模型。填写描述和素材后，勾选本次传输与费用确认，再点击“开始生成”。仅有连接、页面刷新、查询或批准分镜不会启动付费生成。

本机生成任务和结果单独保存在 `data/video-jobs.json`、`data/generated/`，不是旧版模拟分镜。提交前先保存请求标识；相同请求重试不重复提交。超时且无法确认是否接单时显示“提交结果不确定”，先在 OpenRouter 核对，不自动重试扣费。支持远端进度查询、保存真实 MP4、浏览器播放和下载。

## GitHub 云端密钥与生成

在 [仓库 Actions Secrets](https://github.com/yjr927/idore_tools/settings/secrets/actions/new) 新增 **OPENROUTER_API_KEY**，只在 Secret 值中填写密钥，不能写进源码、网页、文案、工作流输入或聊天。GitHub Secrets 会加密保存，网站访客无法读取；有权改动工作流的人仍可能使用密钥，因此应控制仓库写入权限。[GitHub 官方说明](https://docs.github.com/en/actions/concepts/security/secrets)

然后打开 [Generate Seedance video](https://github.com/yjr927/idore_tools/actions/workflows/video-generation.yml)，点 Run workflow。明确选择模型、生成方式和参数，填写描述及所需媒体直链，勾选本次允许使用额度。它只手动触发，不随推送、每日采集或网站刷新自动生成。

云端默认输出竖屏 9:16、无生成音轨；时长与分辨率由模型实时能力校验。结果保存为该次任务的 MP4 和安全摘要附件，保留 7 天，不放入公开的 GitHub Pages。此仓库是公开仓库，工作流输入和有权限访问的任务附件可能被其他 GitHub 用户访问，因此不要把私有角色图、私密描述或带密钥的签名链接填进工作流输入；需要私人素材时使用本机入口。

原任务重新运行会拒绝再次提交新的付费生成。查询或下载失败时，摘要保留 OpenRouter 任务编号；新建一次任务并在 `resume_job_id` 填原编号，只继续查询和下载，不再提交生成。

GitHub Pages 是静态看板，不能读取 Secrets 或自行执行后台接口。本机工作台也不能读回 GitHub 的密钥。两个入口分别使用本机内存密钥和 GitHub Secrets；不要把 GitHub Secret 注入前端构建产物。

## 素材与生成模式

- **文字生成视频**：使用画面、动作、镜头提示词，不包含图片或视频素材。
- **首尾帧**：提供首帧和可选尾帧，模型须明确支持对应能力。适合固定角色的妆前／妆后或换装前／换装后画面。可选择本机 PNG、JPEG、WebP 图片（每张最多 5MB），或 HTTPS 图片直链。
- **角色／动作参考**：提供 1–4 项图片或视频参考，使用支持多模态参考的 Seedance 2 及后续版本。图片可本机选择或提供直链；参考视频需可读取的 HTTPS `.mp4`、`.webm` 或 `.mov` 文件直链。当前不提供本机参考视频上传托管。

TikTok／抖音分享链接是网页，不是媒体文件地址，不能直接交给生成 API。本工具不下载重传原片。应提供有权传给供应商的素材；仅拆解原片动作并使用提示词时，无法承诺逐帧动作、手指细节和节拍一致。

Seedance 的 `frame_images` 与 `input_references` 是不同模式；官方说明两者同时提交时首尾帧优先。本工具要求二选一，避免误以为同时填入就能获得精确首尾帧加动作迁移。[OpenRouter 视频 API](https://openrouter.ai/docs/guides/overview/multimodal/video-generation)

妆前妆后的照片需要先用图像生成／编辑接口制作；这个步骤尚未自动接通。视频生成也不会自动剪辑、加平台音乐或发布 TikTok。生成完成后检查角色、手指、转场、画面与声音，再使用有权发布的音乐制作最终成片。

## OpenAI 文案与 hashtags

发布目标为 TikTok 美国市场，使用英文文案。可以把成片关键帧、内容说明和角色设定交给支持图像输入的 Responses API，生成 2–3 版短文案与 3–5 个相关标签。多个图像可以放在同一请求；分析只覆盖提供画面，不替代完整动作检查。[OpenAI 图像输入](https://developers.openai.com/api/docs/guides/images-vision)

实时标签需要 Work 或合适的数据服务提供美国市场近期的来源 URL、观察时间和趋势内容，再由模型筛选。可用 Web search 补充带来源的信息，但它不等于 TikTok 全站实时热榜。缺少近期依据时只称“相关标签建议”，不能标成当前热门或保证涨播放量。[OpenAI Web search](https://developers.openai.com/api/docs/guides/tools-web-search)

目前 `scripts/analyze_tiktok_candidates.py` 仍只整理候选文字资料；不会看成片、生成发布文案或实时标签。自动造型图、OpenAI 发布草稿和 TikTok 发布是后续独立接入；不因连接 OpenRouter 而自动启用。
