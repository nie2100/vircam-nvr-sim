"""vircam-nvr-sim — 虚拟 ONVIF 摄像头 + 虚拟 NVR 模拟器.

虚拟摄像头 (OnvifCamSimulator): 一台完整支持 ONVIF 协议的 IP 摄像头,
提供 SOAP(Device/Media/PTZ/Events/Imaging) / WS-Discovery / RTSP / 快照.
虚拟 NVR (NvrSimManager + MjpegStreamer): 对真实/虚拟摄像头 RTSP 取流,
解 RFC2435 MJPEG 后转 multipart/x-mixed-replace, 浏览器零插件直接播放.

仅用于开发、测试、教学等合法用途, 详见 README 法律声明.
"""

from .onvif_sim import OnvifCamSimulator
from .nvr_sim import NvrSimManager, NvrDevice, MjpegStreamer

__all__ = ["OnvifCamSimulator", "NvrSimManager", "NvrDevice", "MjpegStreamer"]
__version__ = "1.0.0"
