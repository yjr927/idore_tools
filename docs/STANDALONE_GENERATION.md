# 独立云端工作台

[制作](https://yjr927.github.io/idore_tools/create.html) · [审核](https://yjr927.github.io/idore_tools/review.html) · [连接设置](https://yjr927.github.io/idore_tools/settings.html)

工作台无需启动本机服务。页面显示和审核云端任务，实际动作提取、白模渲染、模型调用及上传在 GitHub Actions 中运行。供应商密钥仅使用 GitHub Secrets；当前入口不使用浏览器 OpenRouter OAuth，也不直接从浏览器生成视频。

## 采集与角色匹配

已保存并重新核对的 Work 任务每天纽约时间上午 9 点运行，每天最多 4 条不同 TikTok 原片：K-pop 最多 2 条，虚拟偶像最多 2 条。访问受阻或证据不足可以少收；不编造流行指标。相同视频当天复查不重复占名额，往日视频复查占当天名额。

Work 提交经过验证的观察后，GitHub 制作队列按男生／女生／混合素材和单人／多人选择已启用角色。这些是素材用途标签，不是对真人身份的推断。分组、人数、变装情况或原片观看未确认时等待；缺同组角色时不会改配另一组。目前只有单人动作提取，多人任务明确等待后续支持。

## 必须经过动作白模

制作过程先在私有临时目录下载并验证完整原片，再用 MediaPipe 提取身体及双手关节点，使用 Blender 渲染全新的无脸白模画面。只把角色图、动作白模及必要的前后造型图传给 Seedance；不把 TikTok 真人原片作为模型参考，不降级为纯文字编舞。

角色图片控制人物外观、服饰与配饰，白模只提供动作顺序、节奏及可观测关节。缺失或遮挡的手指不能当作准确测量；不可见下身保持中性姿态。模型也可能出现手指、身份或动作偏差，需要人工查看成片。

KAI 的首个白模版本已实际完成：720×1280、约 6 秒、24fps、无音轨，OpenRouter 回执费用 $1.66464。[查看生成记录](https://github.com/yjr927/idore_tools/actions/runs/43790641550)。此前直接使用 Bizzyboom 真人原片被供应商以 `InputVideoSensitiveContentDetected.PrivacyInformation` 拒绝；不会通过修改提示词或重复提交绕过。供应商接受一条白模，不代表保证接受所有后续输入。参见 [官方人物参考限制](https://ai.byteplus.com/ark/region:ap-southeast-1/docs/seedance-portrait-asset-guide)。

## 使用网页

1. 在“制作”查看匹配、动作提取、白模、视频与文案状态。刷新不会启动付费请求；手动同步或重试需明确确认额度使用。
2. 在“连接设置”连接当前工作台会话。使用只限 `yjr927/idore_tools` 的细粒度 GitHub 凭证，授予 Actions 读写和 Contents 读取；凭证只存于当前标签页内存。操作仅触发本仓库固定 `main` 工作流。
3. 在“审核”播放原片、动作白模及成片，检查动作与角色细节，编辑英文文案和标签。云端版本改变后必须重新载入，旧审核不能批准新内容。
4. 审核通过后，已接入 TikTok 时才上传草稿；未接入时先保存通过结果。退回不会发布视频。

每次付费提交前先保存任务标记。提交结果不明确、重复点击或页面网络断开时，不自动再发一笔付费请求；先查看已有云端任务。已生成的成片不会因文案失败而丢弃。已提交上传的作品不能通过“重试”重新生成文案或重复创建草稿。

## 英文文案与 hashtags

GitHub Secrets 中的 `OPENAI_API_KEY` 用于成片抽帧分析、美国近期网页检索及结构化英文文案。它分析实际完成的视频，不把原片标题当作成片内容。标签区分内容、品牌与有来源的趋势建议；无近期证据时如实说明，不能承诺实时排名或流量。

当前云端实测 OpenAI 返回 **`insufficient_quota`**。文案与 hashtags 仍受余额或项目额度限制，工作台显示受阻；不会填写假结果或自动换模型。补充余额／调整额度后，再人工重试。文案模型使用 Variable `OPENAI_MODEL`，默认 `gpt-5.4-mini`。视频用 Secret `OPENROUTER_API_KEY`；变装另需 Variable `OPENAI_IMAGE_MODEL` 与角色前后造型要求。

## TikTok 草稿入口

目前尚未完成 TikTok 开发者应用及账号授权，连接按钮保持未接入。正式启用前需申请官方 Content Posting API 的 `video.upload` 权限、完成应用审核和目标账号授权，并配置：

- Secrets：`TIKTOK_ACCESS_TOKEN`、`TIKTOK_ACCOUNT_ID`。
- Variable：`TIKTOK_ACCOUNT_LABEL`，用于审核时显示目标账号。

审核绑定精确视频 SHA-256、文案标签版本和目标账号；换账号或修改内容后需要重新审核。上传使用官方 FILE_UPLOAD 草稿收件箱接口；只有状态 `SEND_TO_USER_INBOX` 才显示“草稿已送达”。处理未完成或回执不确定时保留上传预约，不自动新建第二份草稿。私有回执包含任务编号，不发布到网站；遗失时停止并人工核对。

**该视频草稿 API 不接收文案／hashtags。** 请先在审核页复制文案标签，再进入 TikTok 收件箱粘贴、编辑和确认最终发布。工作台不会自动公开发布。官方说明：[上传视频到草稿](https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video)、[查询处理状态](https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status)。

## 保存范围

成片和封面长期保存到作品区，见 [归档说明](CREATIONS_ARCHIVE.md)。公开仓库中的角色图、白模和成片是公开素材；原片下载、签名地址、关节点临时文件、API 密钥和 TikTok 私有回执不进入 Pages。需要私有素材托管时，需另行准备安全存储，不能把公开仓库当作私有媒体库。
