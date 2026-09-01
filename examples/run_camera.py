#!/usr/bin/env python3
"""启动一台虚拟 ONVIF 摄像头.

默认端点:
  SOAP   http://127.0.0.1:8000/onvif/device_service
  快照   http://127.0.0.1:8000/onvif/snapshot.jpg
  RTSP   rtsp://127.0.0.1:8554/Streaming/Channels/101   (主码流)
         rtsp://127.0.0.1:8554/Streaming/Channels/102   (子码流)
  WS-Discovery UDP 3702 (默认开启, 录像机/搜索工具可发现)

用 VLC / ffplay 打开 RTSP 地址即可看到画面; 默认播放内置 H.264 彩条测试流.
可选 --media 指定图片/视频/.h264 裸流作为画面来源.

用法:
  python run_camera.py
  python run_camera.py --port 9000 --rtsp 9554 --user admin --password secret
  python run_camera.py --media ~/photo.jpg --codec MJPEG
"""
import argparse
import signal
import sys
import time
from pathlib import Path
sys.stdout.reconfigure(line_buffering=True)  # 重定向日志时实时输出

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cam_sim.onvif_sim import OnvifCamSimulator  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="虚拟 ONVIF 摄像头")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000, help="HTTP/SOAP 端口 (默认 8000)")
    ap.add_argument("--rtsp", type=int, default=8554, help="RTSP 端口 (默认 8554)")
    ap.add_argument("--user", default="admin", help="ONVIF 用户名 (默认 admin)")
    ap.add_argument("--password", default="12345", help="ONVIF 密码 (默认 12345)")
    ap.add_argument("--media", default=None,
                    help="画面来源: 图片(jpg/png)/视频(mp4/avi)/.h264 裸流; 默认内置彩条流")
    ap.add_argument("--codec", default="H264", choices=["H264", "H265", "MJPEG"])
    ap.add_argument("--width", type=int, default=1920, help="视频宽 (默认 1920)")
    ap.add_argument("--height", type=int, default=1080, help="视频高 (默认 1080)")
    ap.add_argument("--fps", type=int, default=25, help="帧率 (默认 25)")
    ap.add_argument("--bitrate", type=int, default=4096, help="码率 kbps (默认 4096)")
    args = ap.parse_args()

    sim = OnvifCamSimulator(
        host_ip=args.host,
        http_port=args.port,
        rtsp_port=args.rtsp,
        username=args.user,
        password=args.password,
        media_source=args.media,
        codec=args.codec,
        width=args.width,
        height=args.height,
        fps=args.fps,
        bitrate_kbps=args.bitrate,
    )

    def _stop(*_):
        sim.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    sim.start()
    if not sim.running:
        print(f"[失败] 模拟摄像头启动失败: {sim.start_error}", file=sys.stderr)
        return 1

    print("=" * 58)
    print("  虚拟 ONVIF 摄像头已启动")
    print("=" * 58)
    print(f"  SOAP    {sim.xaddr}   (用户 {args.user}/{args.password})")
    print(f"  快照    {sim.snapshot_url}")
    print(f"  RTSP 主 {sim.rtsp_url}")
    print(f"  RTSP 子 rtsp://{args.host}:{args.rtsp}/Streaming/Channels/102")
    print(f"  WS-Discovery UDP 3702  {'开' if sim.wsd_up else '关'}")
    if sim._media_note:
        print(f"  画面    {sim._media_note}")
    else:
        print("  画面    内置 H.264 彩条测试流 (640x360/25fps)")
    print(f"  ffplay  rtsp://{args.user}:{args.password}@{args.host}:{args.rtsp}/Streaming/Channels/101")
    print("  Ctrl+C 停止")
    print("=" * 58)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
