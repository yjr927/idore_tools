# idore · 趋势素材与角色视频工作台

[趋势素材](https://yjr927.github.io/idore_tools/) · [制作](https://yjr927.github.io/idore_tools/create.html) · [审核](https://yjr927.github.io/idore_tools/review.html) · [连接设置](https://yjr927.github.io/idore_tools/settings.html) · [生成作品](https://yjr927.github.io/idore_tools/gallery.html)

## 当前流程

ChatGPT Work 每天纽约时间上午 9 点采集公开 TikTok 视频：每天最多 **4 条不同原片，K-pop 最多 2 条、虚拟偶像最多 2 条**。原 Work 任务已保存并重新核对；质量不足、登录过期或访问受阻时如实少收，不以示例补足。相同视频当天复查不重复占名额，往日视频复查计入当天。男生／女生素材与单人／多人是独立分类，不增加总名额。详见 [Work 接入说明](docs/CHATGPT_WORK.md)。

Work 通过 GitHub 插件写入 `work-results/latest.json`，云端验证后更新素材库。素材按首次抓取的纽约日期倒序展示；复查保留首次抓取日期，每轮观察保留独立快照。发现链接、观看原片、动作可用与热门证据分别记录，不把发现链接称为已验证热门。

GitHub 云端制作流程读取这些观察，只匹配**同素材分组、同人数**的已启用角色。分类不明或缺角色时等待；目前支持单人，多人任务保留为等待，不冒充多人跟踪已完成。

1. 解析公开原片，在云端私有临时目录下载并验证完整 MP4。
2. 提取身体和双手关节点，渲染不含原片像素、脸部或服饰的新白模动画。
3. 将角色图与动作白模交给 Seedance；变装任务还需该角色的前后造型要求。**真人原片不能直接送入这条生成流程。**
4. 成片保存后抽取实际成片画面，经 OpenRouter 调用 OpenAI 模型搜索美国近期资料，生成英文文案和相关标签，保留真实来源与检索时间。
5. 人工查看视频、修改文案标签并审核。TikTok 接入后，仅将已审核视频送到草稿收件箱；最终编辑与发布仍在 TikTok 内完成。

制作和文案调用会使用对应服务商额度。任务先保存提交记录；结果不确定时停止，不自动重新付费。网站刷新只读取已发布状态。白模的单目深度、遮挡及手指缺失仍可能影响跟拍质量，成片必须人工检查。

## 制作、审核与设置

`create.html` 展示匹配和制作进度；`review.html` 展示原片、动作白模、成片及英文文案标签；`settings.html` 管理当前工作台会话和云端服务状态。浏览与播放无需连接。审核、重试及检查连接通过固定 GitHub 工作流运行，需临时连接仅限本仓库的细粒度 GitHub 凭证：Actions 读写、Contents 读取。它只保留在当前标签页内存；离开、刷新或关闭后需重新连接。

OpenAI 和 OpenRouter 的供应商密钥只使用 GitHub Secrets，不在网页输入、读取或展示。当前入口不使用浏览器 OpenRouter OAuth，也不从浏览器直接提交视频生成。具体操作见 [独立网站说明](docs/STANDALONE_GENERATION.md)。

## 云端配置与真实状态

视频与英文文案共用 Secret `OPENROUTER_API_KEY`。当前工作流固定文案 provider 为 `openrouter`，Variable `OPENROUTER_TEXT_MODEL` 默认 `openai/gpt-5.4-mini`；视频模型 Variable `SEEDANCE_MODEL` 默认 `bytedance/seedance-2.5`。Secret `OPENAI_API_KEY` 仅用于可选的变装造型图，另需 `OPENAI_IMAGE_MODEL` 和角色前后造型要求。网页不提供供应商密钥输入或文案 provider 切换。

此前直接 OpenAI 的实际文案请求返回 `insufficient_quota`；该历史错误保留在原任务中，不改写为成功。文案已迁移为通过 OpenRouter 使用 OpenAI 模型，**新的云端文案实测尚待完成**。连接页将文案、视频及可选造型图分开显示；直接 OpenAI 的额度问题不阻止 OpenRouter 文案请求。以新请求的实际回执和任务状态为准，视频已完成不代表文案也已完成。Work 采集继续使用原任务。

TikTok 尚未建立开发者应用及账号授权，目前只有接入入口与接口实现。正式启用需申请 Content Posting API 的 `video.upload`，完成目标账号授权，再配置 Secrets `TIKTOK_ACCESS_TOKEN`、`TIKTOK_ACCOUNT_ID` 和 Variable `TIKTOK_ACCOUNT_LABEL`。审核绑定精确成片、文案版本和目标账号；私有上传回执不进入网站。视频草稿 API **不能附带文案或 hashtags**，请在审核页复制，进入 TikTok 后粘贴。未接入时只保存审核结果，不声称草稿已送达。

## 作品归档

已完成的 KAI 手势视频和“小鹿 · 接口测试”保存在 `site/generated/`，作品索引位于 `site/data/creations.json`。新云端制作成片会保存到同一作品区；原有手动云端生成任务由独立归档工作流处理。按完整 SHA-256 去重，保留角色和生成记录，不调用额外生成 API。详见 [归档说明](docs/CREATIONS_ARCHIVE.md)。

原片、关节点文件、签名媒体地址、私有上传回执和 API 密钥不发布到 Pages。角色图、动作白模及成片若用于当前公开仓库流程，会作为已授权的公开素材保存；私人角色素材不要直接放入公开清单。

## 保留的工具

匿名 TikTok 公开页采集仅为手动试跑，最多 4 条链接；受阻时保留旧数据并记录失败。它不替代 Work 的登录浏览和观看证据。原有 GitHub 手动视频／造型图任务仍可用于单独实验，不等同于每日生产队列。

本机工作台也保留：需要 Python 3.10+，运行 `python3 server.py --open`，或双击 `start.command`。本机素材库与云端公开素材分别存储。参见 [本机工作台](docs/LOCAL_WORKSPACE.md) 和 [旧接口工具](docs/INTEGRATION.md)。

## 验证

```sh
python3 -m unittest discover -s tests -v
```

云端采集依赖见 `requirements-cloud.txt`，动作提取依赖见 `requirements-motion.txt`。单元测试使用模拟服务，不自动调用付费 API 或上传 TikTok。
