#!/usr/bin/env python3
"""虚拟 NVR: 对一台摄像头(真实或虚拟) RTSP 拉流 → HTML5 MJPEG 转流.

零依赖核心 (nvr_sim.py), 纯标准库实现:
  - RTSP 客户端: OPTIONS/DESCRIBE/SETUP/PLAY, RTP/AVP/TCP interleaved 取流
  - MJPEG 解包: RFC2435 (8B JPEG 头 + fragment offset 分片重组)
  - JPEG 重建: 从 Pillow 样例提取标准 SOF0/DHT/SOS 模板 (无 Pillow 时降级)
  - 转流: multipart/x-mixed-replace → <img> 浏览器原生播放, 零插件

页面:
  http://127.0.0.1:8080/            播放页(主/子码流)
  http://127.0.0.1:8080/stream/main MJPEG 主码流
  http://127.0.0.1:8080/stream/sub  MJPEG 子码流
  http://127.0.0.1:8080/devices     设备状态 JSON

用法:
  python run_nvr_relay.py --source 192.0.2.200 --rtsp 554 --user admin --password xxxx
"""
import argparse
import json
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.stdout.reconfigure(line_buffering=True)  # 重定向日志时实时输出

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cam_sim.nvr_sim import MANAGER  # noqa: E402

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>虚拟 NVR 转流</title>
<style>
  body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Consolas,monospace;margin:0}
  .wrap{max-width:1200px;margin:0 auto;padding:24px}
  h1{font-size:20px;color:#58a6ff;border-bottom:1px solid #21262d;padding-bottom:12px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
  .card h2{font-size:13px;color:#8b949e;margin:0 0 8px}
  img{width:100%;background:#000;border-radius:4px;display:block}
  .meta{color:#8b949e;font-size:12px;margin-top:8px}
  .err{color:#f85149}
</style>
</head>
<body><div class="wrap">
<h1>虚拟 NVR — RTSP → HTML5 MJPEG 转流</h1>
<div class="grid">
  <div class="card"><h2>主码流 /Streaming/Channels/101</h2>
    <img src="/stream/main"><div class="meta" id="m"></div></div>
  <div class="card"><h2>子码流 /Streaming/Channels/102</h2>
    <img src="/stream/sub"><div class="meta" id="s"></div></div>
</div>
<script>
  setInterval(async () => {
    const d = await (await fetch('/devices')).json();
    const dev = d.devices[0];
    if (!dev) return;
    document.getElementById('m').textContent =
      `状态 ${dev.status} | ${dev.main.w}x${dev.main.h} @${dev.main.fps}fps | ${dev.error || ''}`;
    document.getElementById('s').textContent =
      `状态 ${dev.status} | ${dev.sub.w}x${dev.sub.h} @${dev.sub.fps}fps | ${dev.error || ''}`;
  }, 2000);
</script>
</div></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        dev = next(iter(MANAGER.devices.values()), None)  # 真实 NvrDevice 对象
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path.startswith("/stream/"):
            stream = self.path.rsplit("/", 1)[-1]
            if stream not in ("main", "sub"):
                self._send(400, "text/plain", b"bad stream")
                return
            if dev is None:
                self._send(404, "text/plain", b"no device")
                return
            st = dev._get_streamer(stream)
            st.start()
            dev.status = "playing"
            got = st.snapshot(timeout=8.0)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            if got is None:
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                self.wfile.write(b"\r\n")
                return
            frame, last_ts = got
            try:
                while not st._stop.is_set():
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    got = st.snapshot(timeout=2.0, since=last_ts)
                    if got:
                        frame, last_ts = got
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/devices":
            self._send(200, "application/json", json.dumps({"devices": MANAGER.list()}).encode())
        else:
            self._send(404, "text/plain", b"not found")


def main() -> int:
    ap = argparse.ArgumentParser(description="虚拟 NVR: RTSP → HTML5 MJPEG 转流")
    ap.add_argument("--source", required=True, help="摄像头 IP")
    ap.add_argument("--rtsp", type=int, default=554, help="摄像头 RTSP 端口 (默认 554)")
    ap.add_argument("--http", type=int, default=8080, help="本地 HTTP 转流端口 (默认 8080)")
    ap.add_argument("--user", default="admin", help="摄像头用户名")
    ap.add_argument("--password", default="", help="摄像头密码")
    ap.add_argument("--name", default="Camera-1", help="设备显示名")
    args = ap.parse_args()

    dev = MANAGER.add(args.name, args.source, 80, args.rtsp, args.user, args.password)
    dev.start("main")
    dev.start("sub")

    srv = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)

    def _stop(*_):
        MANAGER.remove(dev.id)
        # 不在此处 srv.shutdown(): 会等待活动长连接, 阻塞信号处理; 进程退出由 OS 释放端口
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print("=" * 58)
    print(f"  虚拟 NVR 已启动: {args.name} <- rtsp://{args.source}:{args.rtsp}")
    print(f"  播放页   http://127.0.0.1:{args.http}/")
    print(f"  主码流   http://127.0.0.1:{args.http}/stream/main")
    print(f"  子码流   http://127.0.0.1:{args.http}/stream/sub")
    print("  Ctrl+C 停止")
    print("=" * 58)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
