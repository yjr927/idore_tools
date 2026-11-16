# 视频作品长期归档

已授权将生成的视频保存在 GitHub 仓库及网站作品页。仓库中的 `site/generated/<id>.mp4` 和同名 JPG 为成片及封面；`site/data/creations.json` 是作品索引。相比短期 Actions artifacts，这些文件没有自动到期时间，仍受仓库和网站的可用性及容量限制。单片超过 90 MiB 时停止归档，不会截断或静默压缩。

`Archive completed video creations` 独立监听现有的视频生成及完整造型视频工作流。仅接受本仓库 main 上人工触发的已结束任务，并重新读取 GitHub 元数据核对；允许整体任务失败或取消，因为文案失败之前可能已有已完成视频。归档只认安全摘要中的 `completed` 视频及精确匹配的 `video-<32hex>.mp4`，不会保存原片、白模预览、造型图片、提示词、私有任务状态或密钥。

归档生成封面并检查视频格式、时长和尺寸。稳定 ID 为 `video-` 加成片 SHA-256 的前 24 位，按完整 SHA-256 去重；新增索引保留已有作品和人工分类，生成时间优先采用成片 artifact 时间。缺少角色资料的新片暂记“未标注角色／unknown”，不会猜测性别或人数。完成后直接部署最新 main 的整个网站，并按现有方法合并 Work 观察和分类；不调用视频或文案生成 API。

每个来源任务有独立并发组，避免不同成片互相取消。写入冲突时会重新读取 main、重新合并并有限重试；归档或部署失败可重跑归档任务，不会重跑付费生成。第三方 fork、非 main、非约定工作流、ZIP 穿越／链接、超限文件及不完整摘要会被拒绝。GitHub API 凭证不转发给 artifact 存储地址。

只为已明确授权的两条旧记录提供人工回填入口：

```sh
python scripts/archive_generated_videos.py --run-id 43772946438 --historical --site site
python scripts/archive_generated_videos.py --run-id 43790641550 --historical --site site
```

需由 GitHub 云端设置 `GITHUB_REPOSITORY=yjr927/idore_tools` 与最小权限 `GH_TOKEN`，并安装 ffmpeg。两条回填固定 artifact ID／名称及角色说明；此模式不会扩大自动归档的分支范围。普通未来任务省略 `--historical`。索引仅包含约定的作品元数据、相对媒体路径、原工作流链接和 SHA-256。

**范围限制：**当前自动归档覆盖上述 GitHub 云端入口。浏览器直接向 OpenRouter 生成的视频只有当前浏览器记录；浏览器尚无安全 GitHub 上传／归档通道，因此不能声称它们已永久保存。非 main 上的新生成任务、过期／遗失 artifacts 或未下载成功的远端视频也不会由此自动补回，需要另行明确处理。
