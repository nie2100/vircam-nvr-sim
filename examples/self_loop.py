#!/usr/bin/env python3
"""一站式演示: 虚拟摄像头 → 虚拟 NVR → 浏览器播放 (合并后的完整闭环).

进程内同时运行:
  1. OnvifCamSimulator  — 一台虚拟 ONVIF 摄像头 (127.0.0.1:8000 HTTP / 8554 RTSP)
  2. NvrSimManager      — 虚拟 NVR, 对虚拟摄像头 RTSP 拉流 (RFC2435 解包/JPEG 重建)
  3. ThreadingHTTPServer— MJPEG multipart 转流 + 播放页 (127.0.0.1:8080)

浏览器打开 http://127.0.0.1:8080/ 即可看到主/子码流双画面, 全程无真实设备.
"""
import argparse
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.stdout.reconfigure(line_buffering=True)  # 重定向日志时实时输出

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cam_sim.nvr_sim import MANAGER  # noqa: E402
from cam_sim.onvif_sim import OnvifCamSimulator  # noqa: E402

from run_nvr_relay import PAGE, Handler  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="虚拟摄像头 + 虚拟 NVR 自闭环演示")
    ap.add_argument("--http", type=int, default=8080, help="播放页端口 (默认 8080)")
    ap.add_argument("--cam-port", type=int, default=8000, help="虚拟摄像头 HTTP 端口")
    ap.add_argument("--cam-rtsp", type=int, default=8554, help="虚拟摄像头 RTSP 端口")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="12345")
    ap.add_argument("--codec", default="MJPEG", choices=["H264", "H265", "MJPEG"],
                    help="编码 (默认 MJPEG: NVR 端 RFC2435 解包需 MJPEG 码流)")
    args = ap.parse_args()

    # 1. 虚拟摄像头
    cam = OnvifCamSimulator(host_ip="127.0.0.1", http_port=args.cam_port,
                            rtsp_port=args.cam_rtsp, username=args.user,
                            password=args.password, codec=args.codec)
    cam.start()
    if not cam.running:
        print(f"[失败] 虚拟摄像头启动失败: {cam.start_error}", file=sys.stderr)
        return 1

    # 2. 虚拟 NVR 拉虚拟摄像头的流 (自闭环)
    dev = MANAGER.add("虚拟摄像头", "127.0.0.1", args.cam_port, args.cam_rtsp,
                      args.user, args.password)
    dev.start("main")
    dev.start("sub")

    # 3. MJPEG 转流 + 播放页
    srv = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)

    def _stop(*_):
        cam.stop()
        MANAGER.remove(dev.id)
        # 不在此处 srv.shutdown(): 会等待活动长连接, 阻塞信号处理; 进程退出由 OS 释放端口
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print("=" * 58)
    print("  虚拟摄像头 + 虚拟 NVR 自闭环演示")
    print("=" * 58)
    print(f"  摄像头  SOAP {cam.xaddr}  (用户 {args.user}/{args.password})")
    print(f"  摄像头  RTSP {cam.rtsp_url}")
    print(f"  NVR     拉流: {dev.id}  <- {cam.rtsp_url}")
    print(f"  播放页  http://127.0.0.1:{args.http}/")
    print("  Ctrl+C 停止")
    print("=" * 58)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
