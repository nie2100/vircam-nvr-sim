#!/usr/bin/env python3
"""端到端自测: 虚拟摄像头(SOAP/WS-Discovery/RTSP/快照) + 虚拟 NVR 拉流转帧闭环.

运行: python examples/smoke_test.py
退出码 0 = 全部通过; 1 = 存在失败项.
"""
import base64
import hashlib
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cam_sim.nvr_sim import MANAGER  # noqa: E402
from cam_sim.onvif_sim import OnvifCamSimulator  # noqa: E402

HOST = "127.0.0.1"
PASSED: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        PASSED.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        raise


def _free_port(start: int) -> int:
    for p in range(start, start + 100):
        s = socket.socket()
        try:
            s.bind((HOST, p))
            return p
        except OSError:
            pass
        finally:
            s.close()
    raise RuntimeError("no free port")


def main() -> int:
    http_port = _free_port(28000)
    rtsp_port = _free_port(29000)
    # 用 MJPEG 码流: NVR 端按 RFC2435 解包, 与真实链路一致 (H264 载荷无宽高字段)
    cam = OnvifCamSimulator(host_ip=HOST, http_port=http_port, rtsp_port=rtsp_port,
                            username="admin", password="12345", codec="MJPEG")
    cam.start()
    time.sleep(1.0)
    assert cam.running, cam.start_error

    print("== 虚拟 ONVIF 摄像头 ==")

    # 1. SOAP GetDeviceInformation (WS-UsernameToken 认证)
    def _wsse(user: str, password: str) -> str:
        """构造 ONVIF WS-UsernameToken PasswordDigest 认证头 (与模拟器校验逻辑一致)."""
        nonce_bin = base64.b64decode(base64.b64encode(__import__("os").urandom(16)).decode())
        nonce = base64.b64encode(nonce_bin).decode()
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        digest = base64.b64encode(
            hashlib.sha1(nonce_bin + created.encode() + password.encode()).digest()).decode()
        # 注意: 插值必须在同一个 f-string 片段内, 相邻字符串字面量拼接不会替换 {digest}/{nonce}
        return (
            '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            "<wsse:UsernameToken>"
            f"<wsse:Username>{user}</wsse:Username>"
            f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created>"
            "</wsse:UsernameToken></wsse:Security>"
        )

    def _soap():
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            + _wsse("admin", "12345") +
            "<s:Body><GetDeviceInformation xmlns='http://www.onvif.org/ver10/device/wsdl'/></s:Body>"
            "</s:Envelope>"
        )
        req = urllib.request.Request(
            f"http://{HOST}:{http_port}/onvif/device_service", data=body.encode(),
            headers={"Content-Type": "application/soap+xml"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = r.read().decode()
        except urllib.error.HTTPError as e:
            print(f"  401 body: {e.read()[:160]}", file=sys.stderr)
            raise
        assert "Tingtao" in resp and "Manufacturer" in resp, resp[:200]

    check("SOAP GetDeviceInformation", _soap)

    # 2. 快照 JPEG
    def _snapshot():
        with urllib.request.urlopen(f"http://{HOST}:{http_port}/onvif/snapshot.jpg", timeout=5) as r:
            jpeg = r.read()
        assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9", "非法 JPEG"
        assert len(jpeg) > 2000, f"快照太小: {len(jpeg)}B"
    check("快照 JPEG", _snapshot)

    # 3. RTSP DESCRIBE
    def _rtsp():
        s = socket.create_connection((HOST, rtsp_port), timeout=5)
        s.sendall(f"OPTIONS rtsp://{HOST}:{rtsp_port}/Streaming/Channels/101 RTSP/1.0\r\n"
                  f"CSeq: 1\r\nUser-Agent: SmokeTest\r\n\r\n".encode())
        assert b"200 OK" in s.recv(1024)
        s.sendall(f"DESCRIBE rtsp://{HOST}:{rtsp_port}/Streaming/Channels/101 RTSP/1.0\r\n"
                  f"CSeq: 2\r\nAccept: application/sdp\r\n\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += s.recv(4096)
        assert b"200 OK" in data and b"m=video" in data, data[:200]
        s.close()
    check("RTSP DESCRIBE", _rtsp)

    # 4. WS-Discovery Probe
    def _wsd():
        probe = (
            '<?xml version="1.0"?>'
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(4)
        s.sendto(probe.encode(), (HOST, 3702))
        while True:
            data, addr = s.recvfrom(65535)
            if b"ProbeMatch" in data:
                assert b"XAddrs" in data or b"xaddrs" in data.lower()
                return
    check("WS-Discovery Probe", _wsd)

    print("== 虚拟 NVR 拉流闭环 ==")

    dev = MANAGER.add("虚拟摄像头", HOST, http_port, rtsp_port, "admin", "12345")
    dev.start("main")

    # 5. NVR 取到可解码 JPEG 帧 (MJPEG 码流应带真实分辨率)
    def _nvr_frame():
        got = dev.streamers["main"].snapshot(timeout=10.0)
        assert got is not None, dev.error or "取流超时"
        jpeg, ts = got
        assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9", "NVR 重建 JPEG 非法"
        assert len(jpeg) > 1000
        st = dev.streamers["main"]
        assert st.width > 0 and st.height > 0, f"未解析出分辨率: {st.width}x{st.height}"
    check("NVR RTSP 拉流 → JPEG 帧", _nvr_frame)

    # 6. HTTP MJPEG 转流 (multipart)
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            st = dev.streamers["main"]
            got = st.snapshot(timeout=8.0)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            if got is None:
                return
            frame, ts = got
            try:
                for _ in range(3):
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    got = st.snapshot(timeout=2.0, since=ts)
                    if got:
                        frame, ts = got
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer((HOST, 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _mjpeg():
        with urllib.request.urlopen(f"http://{HOST}:{port}/stream", timeout=8) as r:
            head = r.read(256)
        assert b"multipart/x-mixed-replace" in head or b"--frame" in head
    check("HTTP MJPEG 转流", _mjpeg)

    srv.shutdown()
    cam.stop()
    MANAGER.remove(dev.id)

    print(f"\n全部通过: {len(PASSED)}/{len(PASSED)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
