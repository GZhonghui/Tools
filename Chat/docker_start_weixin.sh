#!/usr/bin/env bash
# 微信桌面：1024×768、96 DPI（100% 缩放）、10 FPS、H.264 CBR，仅开启视频串流。
# 用法：bash ./docker_start_weixin.sh
# 已有同名容器时，先手动停止并删除旧容器，再运行本脚本；继续复用 weixin-config 数据卷。
# 参数修改后需要重建容器，单纯 docker restart 不会更新环境变量。
# Selkies 参数说明：https://docs.linuxserver.io/selkies/user-guide/configuration/
# Docker 资源限制：https://docs.docker.com/engine/containers/resource_constraints/
# 遇到命令失败、未定义变量或管道失败时退出。
set -euo pipefail

# 固定镜像版本，避免无意升级；需要升级时手动修改此处。
image="lscr.io/linuxserver/weixin:1a7800ae-ls50"

# 使用数组保存参数，方便逐项注释；带 | 的值必须保留引号。
docker_args=(
  # 后台运行，启动成功后输出容器 ID。
  -d
  # 容器名称；应与 weixin_unread.py 配置的名称一致。
  --name=weixin
  # CPU 总用量上限相当于 2 个逻辑核，不是绑定指定的两个核。
  --cpus=2
  # /dev/shm 共享内存容量上限，供图形程序使用，并非启动时预占这些内存。
  --shm-size=1000MB
  # 容器内存用量上限。
  --memory=2000MB
  # 内存与 swap 的合计上限：额外最多使用 1000MB swap，须宿主机支持且已配置 swap。
  --memory-swap=3000MB

  # -e 向容器传入环境变量；以下宽、高单位均为像素。
  -e SELKIES_MANUAL_WIDTH=1024
  -e SELKIES_MANUAL_HEIGHT=768
  # 锁定手动分辨率模式，浏览器窗口大小变化时不自动修改桌面尺寸。
  # |locked 是 Selkies 的锁定语法，表示浏览器端不可更改此设置。
  -e "SELKIES_MANUAL_RESOLUTION=true|locked"
  # 显式固定为 96 DPI（100% 界面缩放），防止浏览器连接时按设备像素比切换到 192 DPI 等。
  # 固定分辨率不会自动固定 DPI；此参数显式设置后即锁定，不需要追加 |locked。
  # 同时保持界面尺寸稳定，便于复用 weixin_unread.py 的矩形坐标；闪退是否解决需重建后复测。
  -e SELKIES_SCALING_DPI=96
  # 仅提供 H.264 编码器；单个选项会固定编码器选择。
  -e SELKIES_ENCODER=h264enc
  # 固定使用 CBR 码率控制模式，使下面的视频码率参数生效。
  -e SELKIES_RATE_CONTROL_MODE=cbr
  # 视频目标码率，单位 kbps：初始 2000，浏览器可在 100～2000 之间调整。
  # 2000 kbps = 2 Mbps（约 250 KB/s）；这是编码码率上限，实际网络流量仍有开销和瞬时峰值。
  -e "SELKIES_VIDEO_BITRATE=2000,100-2000"
  # 帧率范围的上下限均为 10，锁定为 10 FPS；单写 10 只设置初始值。
  -e "SELKIES_FRAMERATE=10-10"

  # 浏览器连接后自动开启视频，并锁定此开关。
  -e "SELKIES_VIDEO_ON_START=true|locked"
  # 禁用容器向浏览器传输声音（Audio Stream）。
  -e "SELKIES_AUDIO_ENABLED=false|locked"
  # 禁用浏览器向容器转发麦克风声音。
  -e "SELKIES_MICROPHONE_ENABLED=false|locked"
  # 禁用浏览器向容器转发摄像头画面。
  -e "SELKIES_WEBCAM_ENABLED=false|locked"
  # 禁用手柄输入（Gamepad Input）；普通鼠标、键盘仍可使用。
  -e "SELKIES_GAMEPAD_ENABLED=false|locked"
  # 同时让容器底层跳过虚拟摄像头的创建。
  -e NO_WEBCAM=true
  # 同时禁用容器底层的虚拟手柄支持，并隐藏手柄相关界面。
  -e NO_GAMEPAD=true

  # 宿主机 IP:宿主机端口:容器端口；仅在本机开放 HTTPS 网页 https://127.0.0.1:3001。
  # 截图接口的 8082 是容器内部端口，由检查脚本通过 docker exec 访问，无须映射。
  -p "127.0.0.1:3001:3001"
  # 持久化数据：将命名卷 weixin-config 挂载到 /config，重建容器时继续复用。
  -v weixin-config:/config
)

# 数组展开保留每个参数的完整内容，包括含 | 的配置值。
docker run "${docker_args[@]}" "$image"

