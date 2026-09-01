# vircam-nvr-sim — 虚拟 ONVIF 摄像头 + 虚拟 NVR 模拟器

一套纯 Python 实现的监控设备模拟器：**虚拟摄像头（ONVIF IPC）+ 虚拟 NVR（RTSP → HTML5 MJPEG 转流）**，无需任何真实硬件即可搭建完整的视频监控测试环境。代码源自"听涛"项目的 ONVIF 技能模块，抽出为独立可复用项目。

## 功能特性

### 🎥 虚拟摄像头（`OnvifCamSimulator`）
- **ONVIF SOAP 服务**：Device / Media / PTZ / Events / Imaging 常用操作（ONVIF ver10/ver20）
- **WS-Discovery**：UDP 3702 响应 Probe，录像机 / 搜索工具可直接发现它
- **RTSP 服务**：OPTIONS / DESCRIBE / SETUP / PLAY 完整信令，主/子双码流（`/Streaming/Channels/101`、`/102`）
- **真实视频帧**：录像机可真正解码出画面，无自定义媒体源时按编码分两种画面——
  - **H264 / H265 码流**：内置彩条测试流（640×360 / 25fps，50 帧循环）
  - **MJPEG 码流**：**动态合成特效画面**——8 种主题（赛博扫描 / 矩阵雨 / 极光 / 雷达 / 脉冲 / 星空 / 数据流 / 合成波），叠加身份信息面板、LIVE 徽标、实时时间戳与帧计数；主题按设备 MAC/序列号/IP 稳定散列分配，**同一设备固定特效、不同设备各异**（实测 8/8 渲染正常）
  - 也支持加载本地图片 / 视频 / `.h264` 裸流作为画面来源，H264 / H265 / MJPEG 三种编码
- **快照**：`GET /onvif/snapshot.jpg` → 带时间戳、水印、8 种主题特效的动态 JPEG（Pillow 生成，无 Pillow 时降级内置小图）
- **认证**：WS-UsernameToken + HTTP Digest（默认 `admin/12345`，可配置）
- **故障注入**：`wrong_password` / `slow` / `disable_discovery` / `disable_media`，用于测试录像机添加摄像头失败时的表现

### 🖥️ 虚拟 NVR（`NvrSimManager` + `MjpegStreamer`）
- **RTSP 客户端**：对摄像头（真实或虚拟）OPTIONS / DESCRIBE / SETUP / PLAY 取流，RTP/AVP/TCP interleaved
- **MJPEG 解包**：RFC2435（8B JPEG 头 + fragment offset 分片重组）
- **JPEG 重建**：从样例提取 SOF0/DHT/SOS 模板 + RTP 内嵌量化表 → 完整 JPEG（与 ffmpeg `rtpdec_jpeg.c` 同思路）
- **HTML5 转流**：`multipart/x-mixed-replace` → `<img>` 浏览器原生播放，零插件、零依赖（纯标准库，Pillow 仅可选优化）
- 断线自动重连、主/子码流并发取流、实时帧率/分辨率统计

## 快速开始

```bash
# 依赖: Python 3.8+；Pillow 可选（快照/JPEG 重建增强，缺省自动降级）
pip install pillow          # 可选

# 一键自测（6 项端到端闭环验证）
python examples/smoke_test.py

# 启动一台虚拟 ONVIF 摄像头
python examples/run_camera.py
#   快照  http://127.0.0.1:8000/onvif/snapshot.jpg
#   RTSP  rtsp://127.0.0.1:8554/Streaming/Channels/101
#   默认 H264 码流 = 彩条画面; 加 --codec MJPEG 看动态特效画面(8 主题随机一)
#   ffplay 直接打开上面的 RTSP 地址即可看到画面

# 一站式自闭环演示: 虚拟摄像头 → 虚拟 NVR → 浏览器双画面
python examples/self_loop.py
#   浏览器打开 http://127.0.0.1:8080/

# 虚拟 NVR 拉真实摄像头的流
python examples/run_nvr_relay.py --source 192.0.2.200 --rtsp 554 \
    --user admin --password '你的密码'
#   播放页 http://127.0.0.1:8080/
```

### 摄像头参数示例

```python
from cam_sim.onvif_sim import OnvifCamSimulator

cam = OnvifCamSimulator(
    host_ip="192.0.2.230",   # 对外公布的 IP（网卡实际地址）
    http_port=8000,             # SOAP/快照端口
    rtsp_port=8554,             # RTSP 端口
    username="admin", password="12345",
    model="Demo-Cam-1", manufacturer="Demo",
    media_source="/path/to/video.mp4",  # 可选: 图片/视频/.h264 作为画面
    codec="H264", width=1920, height=1080, fps=25,
)
cam.start()
# ... 测试你的录像机客户端 / ONVIF 工具 ...
cam.stop()
```

### 故障注入示例

```python
# 模拟"密码错误"的摄像头: 所有认证请求一律拒绝
cam = OnvifCamSimulator(host_ip="127.0.0.1", http_port=8000, rtsp_port=8554,
                        fault={"wrong_password": True})
# 慢响应 / 禁用发现 / 禁用 Media 服务同理:
#   fault={"slow": True, "slow_delay": 5}
#   fault={"disable_discovery": True}
#   fault={"disable_media": True}
```

## 项目结构

```
cam_sim/
├── onvif_sim.py        # 虚拟 ONVIF 摄像头（核心, 单文件 2600 行）
├── nvr_sim.py          # 虚拟 NVR: RTSP 拉流 + RFC2435 解包 + JPEG 重建
└── h264_sim_data.py    # 内置 H.264 彩条测试流 (640x360/25fps, 50 帧)
examples/
├── smoke_test.py       # 6 项端到端自测（交付前必跑）
├── run_camera.py       # 启动虚拟摄像头
├── self_loop.py        # 摄像头 + NVR 自闭环演示
└── run_nvr_relay.py    # NVR 拉真实摄像头转流
onvif_wsdl/             # ONVIF WSDL/XSD 本地副本（供 onvif-zeep 等客户端离线使用）
```

## 常见用途

- **开发联调**：编写 / 测试 ONVIF 客户端（添加设备、取流、PTZ、事件订阅）时无需真机
- **录像机验证**：给 NVR / 视频平台添加"虚拟摄像头"，验证接入流程、认证失败、断线重连等边界
- **教学演示**：完整演示 WS-Discovery → SOAP → RTSP → RTP 解包 → 浏览器播放的整条视频链路
- **协议学习**：单文件、零依赖、注释详尽，是学习 ONVIF/RTSP/RFC2435 的最佳活教材

## 法律声明（重要）

> **本项目仅供合法的开发、测试、学习、教学用途。**
>
> 1. **禁止冒充**：禁止将本模拟器用于冒充真实监控设备，实施任何形式的欺诈、诱骗、规避监控、非法取证或侵害他人隐私的行为。模拟器发出的设备标识（Manufacturer/Model/序列号/MAC）均为可配置的演示值，任何将其伪装成真实品牌设备的做法均可能构成违法。
> 2. **使用环境**：请仅在**你拥有或获得授权**的网络环境中使用。向他人网络发送 WS-Discovery 探测、连接未经授权的设备/服务，可能违反当地法律法规。
> 3. **测试边界**：对真实设备的故障注入测试（错误密码、慢响应等）只应在自己的测试环境中进行，勿对生产环境或他人设备发起。
> 4. **用户责任**：你对该项目的一切使用行为负全部责任。使用者须自行确保其用途符合所在国家/地区的法律法规。作者不对任何非法或不当使用造成的后果承担责任。
> 5. **不含恶意代码**：本项目为纯协议实现，不含任何后门、木马、漏洞利用或越权功能。内置测试视频流为 ffmpeg `testsrc2` 生成的彩条画面，不含任何真实影像。

## 许可证

[MIT](LICENSE) © 听涛项目

---

*代码抽取自"听涛"项目（Windows 视频监控运维工具）的 ONVIF 技能模块，原模块已在真实海康/大华设备与多款 NVR 客户端上验证。*
