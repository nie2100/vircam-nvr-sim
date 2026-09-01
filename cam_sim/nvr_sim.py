#!/usr/bin/env python3
"""听涛模拟 NVR: 对摄像头 RTSP 取流 → HTML5 MJPEG 转流 (2026-09-01, 零依赖).

纯标准库实现:
- RTSP 客户端: OPTIONS/DESCRIBE/SETUP/PLAY, RTP/AVP/TCP interleaved 取流
- MJPEG 解包: RFC2435 (8B JPEG 头 + fragment offset 分片重组)
- JPEG 重建: 从 Pillow 样例提取标准 SOF0/DHT/SOS 模板, RTP 内嵌量化表,
  熵数据拼装 → 完整 JPEG (与 ffmpeg rtpdec_jpeg.c 同思路)
- 转流: multipart/x-mixed-replace → <img> 浏览器原生播放(零插件, HTML5)

支持主子码流: /Streaming/Channels/101 主, /102 子。
"""
import io
import logging
import re
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("nvr_sim")

# ── JPEG 重建模板 ─────────────────────────────────────────────
_TEMPLATE: Optional[Dict[str, bytes]] = None
_TEMPLATE_LOCK = threading.Lock()


def _parse_jpeg_segments(data: bytes) -> Dict[int, List[bytes]]:
    """解析 JPEG marker 段, 返回 {marker: [完整段字节(FF xx len body), ...]}."""
    segs: Dict[int, List[bytes]] = {}
    i = 2
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xD8, 0x01):
            i += 2
            continue
        if m in (0xD9,):
            break
        seg_len = (data[i + 2] << 8) | data[i + 3]
        segs.setdefault(m, []).append(data[i:i + 2 + seg_len])
        i += 2 + seg_len
    return segs


def _ensure_template() -> Dict[str, bytes]:
    """用 Pillow 生成样例 JPEG, 提取 APP0/DHT/SOS 完整段 + SOF0 骨架(宽高占位)."""
    global _TEMPLATE
    if _TEMPLATE is not None:
        return _TEMPLATE
    with _TEMPLATE_LOCK:
        if _TEMPLATE is not None:
            return _TEMPLATE
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (10, 20, 40)).save(buf, "JPEG", quality=85)
        segs = _parse_jpeg_segments(buf.getvalue())
        sof0 = segs[0xC0][0]           # FF C0 len(2) precision(1) H(2) W(2) comps(6)
        _TEMPLATE = {
            "sof0_head": sof0[:7],     # FF C0 + len + precision + H(高 2B 占位)
            "sof0_tail": sof0[9:],     # W(宽 2B)之后: 3 分量描述
            "dht": b"".join(segs.get(0xC4, [])),
            "sos": segs[0xDA][0],
            "app0": segs.get(0xE0, [b""])[0],
        }
        return _TEMPLATE


def build_jpeg(w: int, h: int, qtables: bytes, entropy: bytes) -> bytes:
    """RFC2435 载荷 → 完整 JPEG 文件 (SOI + APP0 + DQT + SOF0 + DHT + SOS + 熵 + EOI)."""
    t = _ensure_template()
    sof0 = t["sof0_head"][:5] + struct.pack(">HH", h & 0xFFFF, w & 0xFFFF) + t["sof0_tail"]
    dqt = b""
    if qtables and len(qtables) >= 128:
        dqt = (b"\xff\xdb\x00\x43\x00" + qtables[:64] +
               b"\xff\xdb\x00\x43\x01" + qtables[64:128])
    elif qtables and len(qtables) >= 64:
        dqt = b"\xff\xdb\x00\x43\x00" + qtables[:64]
    return (b"\xff\xd8" + t["app0"] + dqt + sof0 + t["dht"] + t["sos"] +
            entropy + b"\xff\xd9")


def parse_mjpeg_rtp(payload: bytes) -> Tuple[int, int, int, int, Optional[bytes], bytes]:
    """RFC2435 RTP 载荷 → (fragment_off, w, h, q, qtables, entropy_chunk)."""
    if len(payload) < 8:
        return 0, 0, 0, 0, None, b""
    off = (payload[1] << 16) | (payload[2] << 8) | payload[3]
    typ = payload[4]
    q = payload[5]
    w = payload[6] * 8
    h = payload[7] * 8
    p = 8
    qtables = None
    if off == 0 and q > 127:
        # 量化表头: 保留字节, precision, 表长, 表数据
        if p + 4 <= len(payload):
            qtable_len = (payload[p + 2] << 8) | payload[p + 3]
            p += 4
            qtables = payload[p:p + qtable_len]
            p += len(qtables)
    return off, w, h, q, qtables, payload[p:]


# ── RTSP 客户端 ───────────────────────────────────────────────
class CodecUnsupportedError(ConnectionError):
    """码流格式不受支持(如 H264)——永久性错误, 取流线程应报错退出而非重连."""


def _recv_exact(sock: socket.socket, n: int, timeout: float = 8.0) -> bytes:
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("RTSP 连接关闭")
        buf += chunk
    return buf


def _parse_sdp_codec(body: bytes) -> str:
    """从 DESCRIBE 响应 body(SDP)解析视频 codec 名(rtpmap 行, 如 H264/JPEG).

    2026-09-01 BUG-1 修复(DSH 实测反馈): 此前不解析 codec, 拉 H264 码流时
    RFC2435(MJPEG)解包器会把 NAL 载荷误当 JPEG 分片 → 宽高 0、重建"结构合法
    内容乱码"的 JPEG 且完全不报错(静默失败, 冒烟测试都测不出来)。
    """
    text = body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:\d+\s+([A-Za-z0-9]+)", line)
            if m:
                return m.group(1).upper()
    return ""


def _codec_is_mjpeg(codec: str) -> bool:
    """JPEG/MJPEG/MJPG 家族(含 JPEG2000 之外的 JPEG 变体)."""
    return "JPEG" in codec or codec in ("MJPEG", "MJPG")


def _read_rtsp_response(sock: socket.socket, timeout: float = 8.0) -> Tuple[int, Dict[str, str], bytes]:
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("RTSP 响应中断")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    body = rest
    clen = int(hdrs.get("content-length", "0") or 0)
    while len(body) < clen:
        body += sock.recv(clen - len(body))
    return status, hdrs, body[:clen]


def rtsp_open(ip: str, rtsp_port: int, username: str, password: str,
              stream: str = "main", timeout: float = 6.0) -> socket.socket:
    """RTSP 握手并进入 PLAY 状态, 返回已就绪的 TCP socket (interleaved 0-1)."""
    chan = "102" if stream == "sub" else "101"
    base = f"rtsp://{ip}:{rtsp_port}/Streaming/Channels/{chan}"
    auth = ""
    if username:
        import base64 as _b64
        auth = "Authorization: Basic " + _b64.b64encode(
            f"{username}:{password}".encode()).decode() + "\r\n"
    s = socket.create_connection((ip, rtsp_port), timeout=timeout)
    try:
        # OPTIONS
        s.sendall(f"OPTIONS {base} RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: TingtaoNvr\r\n\r\n".encode())
        st, hdrs, body = _read_rtsp_response(s, timeout)
        # DESCRIBE
        s.sendall(f"DESCRIBE {base} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n{auth}\r\n".encode())
        st, hdrs, body = _read_rtsp_response(s, timeout)
        # 码流格式校验(2026-09-01 BUG-1): 非 MJPEG 明确报错, 防 H264 静默乱码
        codec = _parse_sdp_codec(body)
        if codec and not _codec_is_mjpeg(codec):
            raise CodecUnsupportedError(
                f"码流格式 {codec} 不受支持——当前仅支持 MJPEG 码流。"
                f"创建模拟摄像头请指定 codec=MJPEG, 或换用支持 MJPEG 的真实设备")
        # SETUP (TCP interleaved)
        s.sendall((f"SETUP {base}/track1 RTSP/1.0\r\nCSeq: 3\r\n"
                   f"Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n{auth}\r\n").encode())
        st, hdrs, body = _read_rtsp_response(s, timeout)
        sess = hdrs.get("session", "").split(";")[0]
        # PLAY
        s.sendall((f"PLAY {base} RTSP/1.0\r\nCSeq: 4\r\nSession: {sess}\r\n\r\n").encode())
        st, hdrs, body = _read_rtsp_response(s, timeout)
        if st != 200:
            raise ConnectionError(f"RTSP PLAY 失败: {st}")
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        raise


# ── 取流通道: RTSP 拉流 → MJPEG 帧队列 ────────────────────────
class MjpegStreamer:
    """一路摄像头的取流通道(主/子各一). 线程拉 RTSP, 解 RFC2435, 产出 JPEG 帧."""

    def __init__(self, ip: str, rtsp_port: int, username: str, password: str,
                 stream: str = "main", max_queue: int = 3):
        self.ip = ip
        self.rtsp_port = rtsp_port
        self.username = username
        self.password = password
        self.stream = stream
        self.max_queue = max_queue
        self._queue: List[bytes] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self.last_frame_ts = 0.0
        self.width = 0
        self.height = 0
        self.fps_est = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"nvr-{self.ip}:{self.rtsp_port}-{self.stream}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def snapshot(self, timeout: float = 5.0, since: float = 0.0) -> Optional[Tuple[bytes, float]]:
        """取 since 之后的新帧 (无新帧则等待最多 timeout 秒). 返回 (jpeg, 帧时间戳).

        since 用于 MJPEG 输出节流: 调用方记录上一帧时间戳, 只有新帧到达才返回,
        否则会以 CPU 速度重复返回同一帧 (实测 ~1500fps 刷爆带宽).
        """
        deadline = time.time() + timeout
        with self._cond:
            while not self._stop.is_set() and time.time() < deadline:
                if self._queue and self.last_frame_ts > since:
                    return self._queue[-1], self.last_frame_ts
                self._cond.wait(0.2)
        return None

    def _loop(self) -> None:
        """拉流循环: RTSP 连接 + RTP 解包; 断线自动重连(参考 mediamtx/simple-nvr 等成熟实现)."""
        sock = None
        retry = 0
        try:
            while not self._stop.is_set():
                try:
                    if sock is None:
                        sock = rtsp_open(self.ip, self.rtsp_port, self.username, self.password, self.stream)
                        retry = 0
                        if self.error:
                            self.error = None
                            logger.info("取流 %s:%s(%s) 重连成功", self.ip, self.rtsp_port, self.stream)
                        # 分片重组状态
                        frame_off = 0
                        frame_parts: List[Tuple[int, bytes]] = []
                        frame_w = frame_h = 0
                        frame_qtables: Optional[bytes] = None
                        frame_q = 0
                        t0 = time.time()
                        frames = 0
                    # ── 读 RTP/TCP interleaved ──
                    try:
                        hdr = _recv_exact(sock, 4, timeout=3.0)
                    except socket.timeout:
                        continue
                    except OSError as e:
                        # 连接已死: 抛给外层统一重连路径, 不能 break 退出线程
                        raise ConnectionError(f"socket 读取失败: {e}")
                    if len(hdr) < 4 or hdr[0] != 0x24:
                        # 可能是 RTSP 响应(保活), 尝试读响应行
                        try:
                            _read_rtsp_response(sock, 1.0)
                        except Exception as e:
                            raise ConnectionError(f"RTSP 响应读取失败: {e}")
                        continue
                    ln = (hdr[2] << 8) | hdr[3]
                    rtp = _recv_exact(sock, 12, timeout=3.0)
                    payload = _recv_exact(sock, ln - 12, timeout=3.0)
                    marker = bool(rtp[1] & 0x80)
                    off, w, h, q, qtables, chunk = parse_mjpeg_rtp(payload)
                    if off == 0:
                        frame_parts = []
                        frame_off = 0
                        frame_w, frame_h = w, h
                        frame_qtables = qtables
                        frame_q = q
                    if chunk:
                        frame_parts.append((off, chunk))
                    if marker and frame_parts:
                        # 按 off 排序拼接熵数据
                        frame_parts.sort(key=lambda x: x[0])
                        entropy = b"".join(c for _, c in frame_parts)
                        if frame_qtables is None and frame_q <= 99:
                            # 无内嵌表: 用样例默认表(近似)
                            try:
                                from PIL import Image
                                sb = io.BytesIO()
                                Image.new("RGB", (8, 8), (0, 0, 0)).save(sb, "JPEG", quality=frame_q)
                                segs = _parse_jpeg_segments(sb.getvalue())
                                dqt = segs.get(0xDB, [])
                                frame_qtables = b"".join(x[5:69] for x in dqt[:2])[:128]
                            except Exception:
                                pass
                        try:
                            jpeg = build_jpeg(frame_w, frame_h, frame_qtables or b"", entropy)
                        except Exception:
                            continue
                        with self._lock:
                            self._queue.append(jpeg)
                            if len(self._queue) > self.max_queue:
                                self._queue.pop(0)
                        with self._cond:
                            self._cond.notify_all()
                        self.last_frame_ts = time.time()
                        self.width, self.height = frame_w, frame_h
                        frames += 1
                        if frames >= 25:
                            dt = time.time() - t0
                            self.fps_est = frames / max(dt, 0.001)
                            t0 = time.time()
                            frames = 0
                        frame_parts = []
                except CodecUnsupportedError as e:
                    # 码流不受支持(如 H264): 永久错误, 报错退出不重连(重连只会无限刷日志)
                    self.error = str(e)
                    logger.error("取流 %s:%s(%s) 码流不受支持: %s", self.ip, self.rtsp_port, self.stream, e)
                    break
                except (ConnectionError, OSError, socket.timeout) as e:
                    # 断线: 清理连接, 退避重连(不退出线程)
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                    retry += 1
                    self._backoff(e, retry)
                except Exception as e:
                    # 其余异常(解包/重组等)同样进重连, 避免线程静默死亡
                    # (线程死 => 队列停在最后一帧, gen 无限重复旧帧刷带宽)
                    logger.warning("取流 %s:%s(%s) 内部异常: %s", self.ip, self.rtsp_port, self.stream, e)
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                    retry += 1
                    self._backoff(e, retry)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _backoff(self, e: Exception, retry: int) -> None:
        """断线退避等待 (1.5s 递增, 上限 10s), 期间可被 stop 打断."""
        wait = min(10, 1.5 * retry)
        self.error = f"断线重连中({retry}) {type(e).__name__}"
        logger.warning("取流 %s:%s(%s) 断线, %.1fs 后重连: %s",
                       self.ip, self.rtsp_port, self.stream, wait, e)
        end = time.time() + wait
        while time.time() < end and not self._stop.is_set():
            time.sleep(0.2)


# ── 设备管理 ───────────────────────────────────────────────────
class NvrDevice:
    def __init__(self, dev_id: str, name: str, ip: str, http_port: int, rtsp_port: int,
                 username: str = "admin", password: str = ""):
        self.id = dev_id
        self.name = name
        self.ip = ip
        self.http_port = http_port
        self.rtsp_port = rtsp_port
        self.username = username
        self.password = password
        self.streamers: Dict[str, MjpegStreamer] = {}   # main / sub
        self.status = "stopped"   # stopped / playing / error
        self.error: Optional[str] = None
        self._lock = threading.Lock()

    def _get_streamer(self, stream: str) -> MjpegStreamer:
        st = self.streamers.get(stream)
        if st is None:
            st = MjpegStreamer(self.ip, self.rtsp_port, self.username, self.password, stream)
            self.streamers[stream] = st
        return st

    def start(self, stream: str = "main") -> None:
        st = self._get_streamer(stream)
        st.start()
        self.status = "playing"
        self.error = None

    def stop(self, stream: Optional[str] = None) -> None:
        for k, st in self.streamers.items():
            if stream is None or k == stream:
                st.stop()
        if stream is None:
            self.status = "stopped"

    def info(self) -> Dict[str, object]:
        ms, ss = self.streamers.get("main"), self.streamers.get("sub")
        return {
            "id": self.id, "name": self.name, "ip": self.ip,
            "http_port": self.http_port, "rtsp_port": self.rtsp_port,
            "username": self.username,
            "status": self.status,
            "error": self.error or (ms.error if ms else None),
            "main": {"alive": bool(ms and ms._thread and ms._thread.is_alive()),
                     "w": ms.width if ms else 0, "h": ms.height if ms else 0,
                     "fps": round(ms.fps_est, 1) if ms else 0.0,
                     "ts": round(ms.last_frame_ts, 1) if ms else 0.0},
            "sub": {"alive": bool(ss and ss._thread and ss._thread.is_alive()),
                    "w": ss.width if ss else 0, "h": ss.height if ss else 0,
                    "fps": round(ss.fps_est, 1) if ss else 0.0,
                    "ts": round(ss.last_frame_ts, 1) if ss else 0.0},
        }


class NvrSimManager:
    def __init__(self):
        self.devices: Dict[str, NvrDevice] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, name: str, ip: str, http_port: int, rtsp_port: int,
            username: str = "admin", password: str = "") -> NvrDevice:
        with self._lock:
            self._seq += 1
            dev_id = f"dev{self._seq}"
            dev = NvrDevice(dev_id, name, ip, http_port, rtsp_port, username, password)
            self.devices[dev_id] = dev
            return dev

    def remove(self, dev_id: str) -> bool:
        with self._lock:
            dev = self.devices.pop(dev_id, None)
        if dev:
            dev.stop()
            return True
        return False

    def get(self, dev_id: str) -> Optional[NvrDevice]:
        return self.devices.get(dev_id)

    def list(self) -> List[Dict[str, object]]:
        return [d.info() for d in self.devices.values()]


# 全局单例
MANAGER = NvrSimManager()
