# 离线动作白模渲染

`scripts/render_motion_clay.py` 读取提取器的关节 JSON，在 Blender 中生成全新的无脸、无头发、无衣服灰白人形。身体骨长、头部和手掌比例固定；参考片只提供关节方向与时间顺序，不使用原片像素或真人外观。三维深度是单目估计，不能当成标定后的动捕数据。

输入为 `schema_version: 1`、24 fps、6 秒、576×1024、144 帧。`body` 使用左右 shoulder/elbow/wrist/hip/knee/ankle 和 `head_center`；每点为 `[x,y,z,confidence]`，x/y 为原图归一化坐标，y 向下。`hands.left/right` 各为 21 点或 null；不得保存脸部细节。缺少的双臂段不显示；缺手只显示中性掌形，不编造手指运动。不可见的下肢保持标准中性姿势，报告中明确说明。

```sh
python3 scripts/render_motion_clay.py --input motion.json --validate-only
blender -b --python-exit-code 1 --python scripts/render_motion_clay.py -- \
  --input motion.json --output-dir clay-frames
ffmpeg -framerate 24 -i clay-frames/frame-%06d.png -frames:v 144 \
  -c:v libx264 -pix_fmt yuv420p -an -movflags +faststart clay-motion.mp4
```

无显示设备的 Linux 需由现有工作流提供 Xvfb 和软件 OpenGL；采用 `BLENDER_WORKBENCH`，无需额外 Python 包。脚本输出 `frame-000000.png` 至 `frame-000143.png` 及 `render-summary.json`，输出目录必须没有旧帧。可用 `--frame-limit 1` 先检查单帧；这种输出不是完整动作参考。

镜头固定，按整段头部到上大腿和手部活动范围构图；不以推测出的膝／脚踝拉远画面。手指各节按观测方向驱动，骨长统一，手部遮挡、深度估计及检测丢失仍会影响保真度。完整片需先比对关键帧和连续动作，再决定是否用于生成。渲染器不联网、不上传、不给 Seedance 提交任务，也不保证任何供应商接受白模参考。
