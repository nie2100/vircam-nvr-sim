"""
OnvifCamSimulator — ONVIF 摄像头模拟器 (2026-08-18 新增, 听涛 ONVIF 技能核心).

模拟一台支持 ONVIF 协议的 IP 摄像头 (IPC):
- SOAP 服务: Device / Media / PTZ / Events / Imaging (ONVIF ver10/ver20 常用操作)
- WS-Discovery: UDP 3702 响应 Probe, 让录像机/搜索工具能发现它
- RTSP 服务: 响应 OPTIONS/DESCRIBE/SETUP/PLAY, 提供可拉流的地址(SDP, 无真实 RTP 负载)
- 快照: GET /onvif/snapshot → JPEG 测试图(Pillow 生成, 带时间戳/文字)
- 认证: WS-UsernameToken + HTTP Digest(默认 admin/12345, 可配置)
- 故障注入: wrong_password(认证一律失败) / slow(响应延迟) / disable_discovery /
  disable_media(Media 服务报错) — 用于测试录像机添加摄像头失败时的表现

跨平台(Windows 原生 + WSL)。依赖: 标准库 + Pillow(快照, 无则降级内置小图)。

使用:
    sim = OnvifCamSimulator(host_ip="192.0.2.230", http_port=8000, ...)
    sim.start()          # 起 HTTP/WS-Discovery/RTSP 线程
    ... 测试 ...
    sim.stop()
    sim.request_log      # 收到的 ONVIF/RTSP 请求日志
"""

import base64
import hashlib
import json
import logging
import math
import os
import random
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("tingtao.onvif_sim")

# ── ONVIF 命名空间 ──────────────────────────────────────────────
NS = {
    "soap": "http://schemas.xmlsoap.org/soap/envelope/",
    "soap12": "http://www.w3.org/2003/05/soap-envelope",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "xsd": "http://www.w3.org/2001/XMLSchema",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "tt": "http://www.onvif.org/ver10/schema",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "trptz": "http://www.onvif.org/ver10/ptz/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "timg": "http://www.onvif.org/ver10/imaging/wsdl",
    "dn": "http://www.onvif.org/ver10/network/wsdl",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
    "xmime": "http://www.w3.org/2005/05/xmlmime",
}
SOAP_NS = NS["soap"]
SOAP12_NS = NS["soap12"]

# 注册命名空间前缀, 让 ET 序列化输出标准前缀(soap:Envelope/tds:...)而非 ns0/ns1
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

_ET_NS_CLEAN = re.compile(r"\{[^}]*\}")


def _q(tag: str, ns: Optional[str] = None) -> str:
    """构造 {ns}tag 形式, 用于 ElementTree."""
    if ns:
        return f"{{{ns}}}{tag}"
    return tag


def _child(parent: ET.Element, tag: str, ns: Optional[str] = None, text: Any = None) -> ET.Element:
    el = ET.SubElement(parent, _q(tag, ns))
    if text is not None:
        el.text = str(text)
    return el


def _soap_response(body_el: ET.Element, soap12: bool = False) -> bytes:
    """包装 SOAP Envelope (用请求同版本命名空间)."""
    env_ns = SOAP12_NS if soap12 else SOAP_NS
    env = ET.Element(_q("Envelope", env_ns))
    header = ET.SubElement(env, _q("Header", env_ns))
    body = ET.SubElement(env, _q("Body", env_ns))
    body.append(body_el)
    xml = ET.tostring(env, encoding="unicode")
    return xml.encode("utf-8")


def _soap_fault(fault_string: str, soap12: bool = False) -> bytes:
    env_ns = SOAP12_NS if soap12 else SOAP_NS
    env = ET.Element(_q("Envelope", env_ns))
    body = ET.SubElement(env, _q("Body", env_ns))
    fault = ET.SubElement(body, _q("Fault", env_ns))
    code = ET.SubElement(fault, _q("Code", env_ns))
    _child(code, "Value", env_ns, "env:Sender")
    reason = ET.SubElement(fault, _q("Reason", env_ns))
    txt = ET.SubElement(reason, _q("Text", env_ns))
    txt.set("xml:lang", "zh-CN")
    txt.text = fault_string
    detail = ET.SubElement(fault, _q("Detail", env_ns))
    _child(detail, "FaultString", env_ns, fault_string)
    return ET.tostring(env, encoding="utf-8")


def _localname(tag: str) -> str:
    return _ET_NS_CLEAN.sub("", tag)


# ── 小图生成(无 Pillow 时的降级) ───────────────────────────────
_JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)

# 2026-08-19 修复(DEBUG/ONVIF 报告: 模拟摄像头快照 1x1 白图): 内置一张真实
# 640x360 JPEG 快照(彩条+文字)——Pillow 缺失/打包遗漏时用它, 不再降级 1px。
_JPEG_FALLBACK = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEP"
    "ERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4e"
    "Hh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCAFoAoADASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5xopa"
    "K9480SilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASiloo"
    "ASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASil"
    "ooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooAS"
    "ilooASilooAXFGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdij"
    "FADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinY"
    "oxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp"
    "2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FG"
    "KdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANx"
    "RinYoxQA3FGKdijFAC4oxTsUYoAbivZPh34i8QT2/wANbebXdUlhl8aSQSRvdyFXjQ6cVQgnBUbm"
    "wOgyfWvHcUYqZR5ik7Hu/wANvL1ib4VeH5Ngu7LytV09jwW26tc/aY8+8aK494cD71ZNhYaxd6LJ"
    "Dpl1cw+F/wDhFHmRPsvnWU1xHaNJcCQ71CziZJCrYZxhDjaK8exRip9nruPnPTdPvvEHiDwH4U0O"
    "48QaqbW916/trnddyMot0hsCdwJwURS7AdBzXRazdy+INCvPFngaTUZNdunSzUxW/k3awwtI0ixL"
    "G7kBY5bFPlOSkb9t1eIYoxQ6Ycx6cz3sXxB8PyTWP27xEukv/akHnCK4e4KzhSHIOLkQmFhwW80D"
    "ILZB7DRobmLXNBku7u+nnm8VeG3ddQiCXsPz6goS4xyz4UEMeShTgdK8BxRih07gpWPaNW8qHwF4"
    "1v4tm7xTp0GrsB/CFvLIHb7efNdJ/wBsqxNRHiRNDtjohx4QPh9fN83P2IzeR+/3/wAP2j7Rv2Z+"
    "bPl/w4rzLFGKFTsHMe6+CbexbU/B/hqGe5fU/Dmq6VfzRPbKqR+dcp9oAcOSz7p7dSCq4EHfGapa"
    "t5UPgLxrfxbN3inToNXYD+ELeWQO328+a6T/ALZV4vijFL2Wt7hzjcUYp2KMVqQNxRinYoxQA3FG"
    "KdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANx"
    "RinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFAD"
    "cUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQ"
    "A3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFAC4oxS0UA"
    "JijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0UAJijFLRQ"
    "AmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtF"
    "ACYoxS0UAJijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0"
    "UAJijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0UAJijFLRQAmKMUtFACYoxS0UAOopaK"
    "AEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEop"
    "aKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAE"
    "opaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaKAEopaK"
    "AEopaKAHUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLijFA"
    "CUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLijFACUUuKMU"
    "AJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4ox"
    "QAlFLijFACUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLijFACUUuKMUAJRS4oxQAlFLij"
    "FACUUuKMUAa39j/9PH/jn/16P7H/AOnj/wAc/wDr1rUV+a/6wZh/z8/CP+R/U3/EOOG/+gb/AMnq"
    "f/JGT/Y//Tx/45/9ej+x/wDp4/8AHP8A69a1FH+sGYf8/Pwj/kH/ABDjhv8A6Bv/ACep/wDJGT/Y"
    "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"
    "aqrVp1F9pt/exMUYpaKzMBMUYpaKAExRilooATFGKWigBMUYpaKAExRilooATFGKWigBMUYpaKAE"
    "xRilooAWinUUANop1FADaKdRQA2inUUANop1FADaKdRQA2inUUANop1FADaKdRQA2inUUANop1FA"
    "DaKdRQA2inUUANop1FADaKdRQA2inUUANop1FADaKdRQA2inUUANop1FADaKdRQA2inUUANop1FA"
    "DaKdRQA2inUUANop1FADaKdRQA2inUUANop1FADaKdRQA2inUUANop1FADaKdRQA2inUUANop1FA"
    "DaKdRQA2inUUANop1FADaKdRQA2inUUALRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTs"
    "UYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoA"
    "bRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTs"
    "UYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoA"
    "bRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAbRTsUYoAXFGKdijFADcUYp2KMUANxRinYoxQ"
    "A3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KM"
    "UANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdi"
    "jFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRin"
    "YoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFADcUY"
    "p2KMUANxRinYoxQA3FGKdijFADcUYp2KMUANxRinYoxQA3FGKdijFAC4oxS4oxQAmKMUuKMUAJij"
    "FLijFACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4oxQAmKMU"
    "uKMUAJijFLijFACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4"
    "oxQAmKMUuKMUAJijFLijFACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4oxQAmKMUuKMUAJijFLij"
    "FACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4oxQAmKMUuKMU"
    "AJijFLijFACYoxS4oxQAmKMUuKMUAJijFLijFACYoxS4oxQA6ilooASilooASilooASilooASilo"
    "oASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASi"
    "looASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooASilooA"
    "SilooASilooASilooASilooASilooASilooASilooASilooASilooASilooAdRS0UAJRS0UAJRS0"
    "UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJR"
    "S0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UA"
    "JRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAJRS0UAOxRi"
    "vGaK4/rfkdHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPr"
    "fkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9m"
    "xRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKP"
    "rfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9"
    "mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaK"
    "PrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM"
    "9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGa"
    "KPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsP"
    "M9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPM9mxRivGaKPrfkHsPMKKKK4zo"
    "Ct5fB3iN4rOVNPDi8dEiCzxllLoZE8xQ2YtyAuC4UFQT0BNYNemabrGhaJaafZQeJLa8t7uGZdVl"
    "WK4MyzzWc1ujkPGB5cAmIAVixJYgYIC3FJ7iba2OPTwlrrao+nC2txKkC3Bka8hWDymICuJi/llS"
    "WCghuScdeKQ+EvEK2d3dSacY1s2lSaOSVElBiOJMRk72CH7xUELznGDXU2d34Ymv9KtL7X7RrPRN"
    "JEIMkVyINRn+0SzBDtjLiNTMMllBIjwMZBFq88W6bqGiN/a2pafdXMNlqFvNtsn+03dzNNPLHPFL"
    "sGyPdOpZSyEqrAqcgVXLEV2cPo3hzWdYtJrrTrRZYomKktMiF2CliiKxBkbaCdqAn25FOuvDGt2u"
    "kf2pNaxrbiKOdgLmNpY4pMbJHiDb0Rty4ZlAO5efmGet8I+JdAii0K5uja6O2ga02qC0hSeQXqlI"
    "PkQkvh91uAdzKuJOOmKm1XxXpMvgqSyS8sp1k0uwtY7L7G63XnQtEZfPuAoLwHZJtQSHGYsBdnC5"
    "Y23C7ucPZeH9YvdBv9etrJ302w2/abgsqquXRMAE5Y7pEyFzjcCcA0/WvDmsaNbpPqNqkSM/lsFn"
    "jkaJ8Z2SKrExtj+FwDweODV3RdWsYtN8WxyJFYnUdMSG0t4hIyeYL21kKgsWI+SJzlj/AA4zkgVv"
    "/EHxLo9/oN7bWM1ld3WqapFqM9xDbyxSnZHKMz7/AJfNYzkkRfJlSR96lZWC7ucw/hLxElvpE76X"
    "Kqaw7LYZZd0pUIxO3OVG2RGywAIYEHHNVtQ0LVbHVLfTZrXzLq5CG2W3kWdZw5wvltGWV8nj5SeQ"
    "R1FdX4M8TaXpekaFBczRmaC81gTLLCzpEl1ZQQRSMAPmUMrkqMnCdORnN8b6lZ3cVrHp+p20hhgh"
    "S8W0heC3uJw87CWKPYqqER1U/KpLMxAO5jQ1GwXdzJ1Pw7rOneIU8P3Viw1NzCEt43WQsZVV48FS"
    "Qcq6ng96sW/hHxBPrWo6OtlGl5ps5t7tZbmKNI5Q+zZvZghYsCAATnHGa3bnxTolv4kfUBaXOoB9"
    "BsLCOW3ufs728iWUEMxUvG+T8kiZx3JB6GtfxVr3h3xBrfiq1XxEbXT9S1pNSsrmZJpBHGk11ujw"
    "Iwyl/tBlUBcDIDNuyafLHuF2cK3hrV49KfUp47W3gR5EK3F7DFKWjOHCxM4diCccKeeKyK9P1zxZ"
    "oOuQareXsuneRcPqUsWnvpzfbFlmkkkgZJwCoVWdC3zJkKylW4J8wqZJLYabe4UUUVIwooooAKKK"
    "KACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAoooo"
    "AKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigA"
    "ooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACi"
    "iigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKK"
    "KACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAoooo"
    "AKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigA"
    "ooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACi"
    "iigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD/9k="
)


def _gen_snapshot_jpeg(text: str = "ONVIF SIM CAM", width: int = 640, height: int = 360) -> bytes:
    """生成带文字的快照 JPEG (Pillow, 失败降级内置真实 JPEG). 2026-09-01: 宽高可配."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        width = max(64, int(width))
        height = max(64, int(height))
        img = Image.new("RGB", (width, height), (24, 48, 96))
        draw = ImageDraw.Draw(img)
        # 渐变背景条
        for y in range(height):
            shade = int(24 + (y / height) * 40)
            draw.line([(0, y), (width, y)], fill=(shade, shade + 16, shade + 48))
        draw.rectangle([0, height - 48, width, height], fill=(8, 8, 16))
        try:
            font = ImageFont.truetype("arial.ttf", 28)
            font_small = ImageFont.truetype("arial.ttf", 18)
        except Exception:
            font = ImageFont.load_default()
            font_small = font
        draw.text((24, 30), text, fill=(255, 255, 255), font=font)
        draw.text((24, height - 36), time.strftime("%Y-%m-%d %H:%M:%S"), fill=(200, 220, 255), font=font_small)
        draw.text((width - 220, 30), "TEST IMAGE", fill=(120, 200, 120), font=font_small)
        buf = __import__("io").BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        # 2026-08-19: Pillow 缺失时用内置真实 JPEG(不再 1x1 白图)
        return _JPEG_FALLBACK


# ── 动态 HUD 快照 (2026-09-01: 8 主题炫酷动效 + 摄像头身份信息) ────────
# 设计原则: 零新依赖(纯 Pillow), 静态层(背景+信息面板)一次性渲染缓存,
# 动态层每帧仅 3~8 个 draw 元素(线条/椭圆/文字), 640x360 帧成本 <3ms,
# JPEG 编码后总帧成本 10~20ms, 满足 25fps 预算。
_HUD_FONT_CJK = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc", "/mnt/c/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf", "/mnt/c/Windows/Fonts/simhei.ttf",
]
_HUD_FONT_MONO = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/consola.ttf", "/mnt/c/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]
_HUD_THEME_NAMES = ["cyber_scan", "matrix", "aurora", "radar",
                    "pulse", "starfield", "dataflow", "synthwave"]
_HUD_CYAN = (60, 220, 255)
_HUD_GREEN = (80, 240, 140)
_HUD_AMBER = (255, 200, 80)


def _hud_font(scale: float, mono: bool = False, size: int = 14):
    """按 scale 取字体; 中文字体缺失时回退 DejaVu/默认(中文显示方块但可用)."""
    from PIL import ImageFont
    px = max(8, int(size * scale))
    for p in (_HUD_FONT_MONO if mono else _HUD_FONT_CJK):
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            continue
    try:
        return ImageFont.load_default(px)
    except Exception:
        return ImageFont.load_default()


def _hud_has_cjk(s: str) -> bool:
    """文本是否含 CJK 字符(中/日/韩) — 等宽字体(DejaVu/Consolas)无 CJK 会渲染成方块."""
    return any('\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f'
               or '\uff00' <= c <= '\uffef' or '\uac00' <= c <= '\ud7af'
               for c in s)


def _hud_val_font(v, mono_font, cjk_font):
    """信息面板值字体: 含 CJK 用中文字体, 纯 ASCII 保持等宽(2026-09-01 用户反馈 DEV 中文变方块)."""
    return cjk_font if _hud_has_cjk(v) else mono_font


def _hud_pick_theme(sim) -> str:
    """按 MAC/序列号/IP 稳定散列选主题: 同一摄像头固定特效, 不同摄像头各异."""
    seed_src = (sim.mac or "") + (sim.serial or "") + (sim.host_ip or "")
    idx = int.from_bytes(hashlib.md5(seed_src.encode("utf-8")).digest()[:4], "big") % len(_HUD_THEME_NAMES)
    return _HUD_THEME_NAMES[idx]


# ── 8 个特效: 每个 (static(w,h,rng)->Image 背景, anim(d,frame,w,h,rng) 每帧动画) ──
def _hud_static_cyber(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):                      # 暗蓝渐变
        v = int(10 + 24 * y / h)
        d.line([(0, y), (w, y)], fill=(v, v + 8, v + 30))
    for gx in range(0, w, 48):              # 网格
        d.line([(gx, 0), (gx, h)], fill=(24, 52, 96))
    for gy in range(0, h, 48):
        d.line([(0, gy), (w, gy)], fill=(24, 52, 96))
    d.line([(0, 0), (w, h)], fill=(30, 70, 130))     # 斜线装饰
    d.line([(0, h), (w, 0)], fill=(30, 70, 130))
    return img


def _hud_anim_cyber(d, frame, w, h, rng):
    y = (frame * 3) % (h + 20) - 10         # 扫描线
    d.line([(0, y), (w, y)], fill=(70, 230, 255), width=2)
    d.line([(0, y - 4), (w, y - 4)], fill=(35, 120, 150))
    d.line([(0, y + 4), (w, y + 4)], fill=(35, 120, 150))
    cx = int(w * 0.5 + w * 0.32 * math.sin(frame * 0.021))   # Lissajous 光斑
    cy = int(h * 0.30 + h * 0.18 * math.cos(frame * 0.017))
    for r, a in ((44, 60), (26, 110), (12, 200)):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(60, 220, 255, a), width=2)


def _hud_static_matrix(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (2, 8, 4))
    d = ImageDraw.Draw(img)
    for gx in range(0, w, 18):              # 数字雨列轨道(暗)
        d.line([(gx, 0), (gx, h)], fill=(4, 26, 12))
    return img


def _hud_anim_matrix(d, frame, w, h, rng):
    n = max(2, w // 140)
    for i in range(n):
        col = rng.randint(0, w - 4)
        speed = rng.randint(2, 6)
        head = (frame * speed + i * 97) % (h + 80) - 40
        d.line([(col, head - 18), (col, head)], fill=(40, 200, 90), width=2)
        d.line([(col, head), (col, head + 2)], fill=(160, 255, 200), width=2)


def _hud_static_aurora(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (6, 8, 20))
    return img


def _hud_anim_aurora(d, frame, w, h, rng):
    # 3 条正弦彩色光带(40 采样点 polyline, 便宜)
    cols = ((0, 220, 180), (120, 120, 255), (255, 90, 180))
    for band, col in enumerate(cols):
        phase = frame * 0.05 + band * 2.1
        amp = h * (0.08 + 0.05 * band)
        pts = []
        for x in range(0, w + 1, max(4, w // 40)):
            yy = h * 0.35 + band * h * 0.12 + amp * math.sin(x * 0.02 + phase)
            pts.append((x, int(yy)))
        d.line(pts, fill=col, width=3)
        d.line([(x, y + 4) for x, y in pts], fill=tuple(max(0, c // 3) for c in col), width=1)


def _hud_static_radar(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (4, 10, 12))
    d = ImageDraw.Draw(img)
    cx, cy, r = w // 2, int(h * 0.42), int(min(w, h) * 0.30)
    for rr in (r, r * 2 // 3, r // 3):
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=(0, 90, 100), width=1)
    d.line([(cx - r, cy), (cx + r, cy)], fill=(0, 90, 100))
    d.line([(cx, cy - r), (cx, cy + r)], fill=(0, 90, 100))
    return img


def _hud_anim_radar(d, frame, w, h, rng):
    cx, cy, r = w // 2, int(h * 0.42), int(min(w, h) * 0.30)
    ang = frame * 0.04
    x2 = int(cx + r * math.cos(ang)); y2 = int(cy + r * math.sin(ang))
    d.line([(cx, cy), (x2, y2)], fill=(0, 220, 200), width=2)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(0, 255, 220))
    for i in range(3):                      # 回波点
        ta = frame * 0.01 + i * 2.1
        px = int(cx + r * 0.6 * math.cos(ta)); py = int(cy + r * 0.5 * math.sin(ta))
        rr = 3 + (frame // 8 + i) % 4
        d.ellipse([px - rr, py - rr, px + rr, py + rr], outline=(80, 255, 220), width=1)


def _hud_static_pulse(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (8, 8, 16))
    d = ImageDraw.Draw(img)
    d.line([(0, h // 2), (w, h // 2)], fill=(30, 30, 60))
    return img


def _hud_anim_pulse(d, frame, w, h, rng):
    for wave, col in ((0, (0, 200, 255)), (1, (200, 60, 255))):
        phase = frame * 0.08 + wave * math.pi
        pts = [(x, int(h * 0.5 + h * 0.18 * math.sin(x * 0.03 + phase)))
               for x in range(0, w + 1, max(4, w // 48))]
        d.line(pts, fill=col, width=2)
        mx = int(((frame * 3) % (w + 40)) - 20)
        my = int(h * 0.5 + h * 0.18 * math.sin(mx * 0.03 + phase))
        d.ellipse([mx - 5, my - 5, mx + 5, my + 5], fill=(255, 255, 255))


def _hud_static_starfield(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        v = int(4 + 18 * y / h)
        d.line([(0, y), (w, y)], fill=(v, v + 4, v + 20))
    for _ in range(60):                     # 静态星
        x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
        b = rng.randint(60, 200)
        d.point((x, y), fill=(b, b, b + 30))
    return img


def _hud_anim_starfield(d, frame, w, h, rng):
    for i in range(14):                     # 移动粒子(预生成参数, 每帧仅算位移)
        px = (frame * (rng.randint(1, 4)) + i * 61) % w
        py = (frame * (rng.randint(2, 6)) + i * 37) % h
        b = 120 + rng.randint(0, 130)
        d.rectangle([px, py, px + 2, py + 2], fill=(b, b, 255))
        d.line([(px - 6, py), (px, py)], fill=(b // 3, b // 3, b // 3 + 60))


def _hud_static_dataflow(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (6, 10, 16))
    d = ImageDraw.Draw(img)
    for gx in range(0, w, 40):
        d.line([(gx, 0), (gx, h)], fill=(14, 30, 44))
    return img


def _hud_anim_dataflow(d, frame, w, h, rng):
    for i in range(6):
        y = rng.randint(10, h - 10)
        ln = rng.randint(30, 90)
        x0 = (frame * rng.randint(2, 5) + i * 173) % (w + ln) - ln
        col = rng.choice(((0, 200, 255), (255, 200, 80), (80, 240, 140)))
        d.line([(x0, y), (x0 + ln, y)], fill=col, width=2)
        if rng.random() < 0.4:
            d.rectangle([x0 + ln + 4, y - 3, x0 + ln + 10, y + 3], fill=col)


def _hud_static_synth(w, h, rng):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):                      # 暗紫渐变 + 地平线辉光
        t = y / h
        v = int(8 + 20 * t)
        d.line([(0, y), (w, y)], fill=(v, v, v + 34))
    hy = int(h * 0.68)
    for y in range(hy, h):                  # 地平线下暖色
        t = (y - hy) / max(1, h - hy)
        d.line([(0, y), (w, y)], fill=(int(20 + 60 * t), int(10 + 20 * t), int(40 + 90 * t)))
    for i in range(10):                     # 透视网格(间距递增)
        yy = hy + int((i / 10.0) ** 2 * (h - hy))
        d.line([(0, yy), (w, yy)], fill=(200, 60, 160))
    return img


def _hud_anim_synth(d, frame, w, h, rng):
    hy = int(h * 0.68)
    sun_r = int(min(w, h) * 0.10)
    sx, sy = w // 2, hy
    glow = 120 + int(60 * math.sin(frame * 0.05))       # 太阳呼吸
    d.ellipse([sx - sun_r, sy - sun_r, sx + sun_r, sy + sun_r], fill=(255, 200, 120, 90))
    d.ellipse([sx - sun_r // 2, sy - sun_r // 2, sx + sun_r // 2, sy + sun_r // 2],
              fill=(255, 230, 160, glow))
    for i in range(4):                      # 地平线移动高光
        gx = (frame * 2 + i * 90) % (w + 60) - 30
        d.line([(gx, hy - 2), (gx + 40, hy + 2)], fill=(255, 120, 220), width=2)


_HUD_THEMES = {
    "cyber_scan": (_hud_static_cyber, _hud_anim_cyber),
    "matrix":     (_hud_static_matrix, _hud_anim_matrix),
    "aurora":     (_hud_static_aurora, _hud_anim_aurora),
    "radar":      (_hud_static_radar, _hud_anim_radar),
    "pulse":      (_hud_static_pulse, _hud_anim_pulse),
    "starfield":  (_hud_static_starfield, _hud_anim_starfield),
    "dataflow":   (_hud_static_dataflow, _hud_anim_dataflow),
    "synthwave":  (_hud_static_synth, _hud_anim_synth),
}


def _hud_draw_info(img, d, sim, w, h, scale):
    """信息面板(左下, 半透明) + 标题栏(顶部) + LIVE/特效标签. 所有主题统一.

    2026-09-01 修复: 面板宽度按内容 textlength 动态计算(防文字出框),
    行高 17px 防重叠, 面板半透明(alpha overlay 合成), 超宽值自动截断。
    """
    from PIL import Image, ImageDraw
    mono_s = _hud_font(scale, mono=True, size=12)
    mono_m = _hud_font(scale, mono=True, size=15)
    cjk = _hud_font(scale, mono=False, size=15)
    # 顶部标题栏
    bh = int(34 * scale)
    d.rectangle([0, 0, w, bh], fill=(8, 12, 22))
    d.line([(0, bh), (w, bh)], fill=_HUD_CYAN)
    dev = str(sim.device_name or "")
    d.text((10, bh // 2 - 10), dev, fill=(230, 245, 255), font=cjk)
    # LIVE 徽标(右上标题栏)
    lw = int(66 * scale)
    d.rectangle([w - lw - 8, 6, w - 6, bh - 6], outline=(80, 220, 255))
    d.ellipse([w - lw + 2, 12, w - lw + 10, 20], fill=(255, 60, 60))
    d.text((w - lw + 14, 8), "LIVE", fill=(120, 240, 255), font=mono_m)
    # 信息面板(左下, 两列×6行: 短值列 + 长值列, 紧凑间距, 半透明)
    dev_s = dev[:14]
    model_s = str(sim.model or "")[:20]
    rtsp_s = str(sim.rtsp_url).replace("rtsp://", "")   # 完整取流地址(含路径)
    br_s = f"{max(1, sim.bitrate_kbps // 1024)}Mbps"
    rows = [
        ("RES", f"{sim.video_width}x{sim.video_height}", "IP", f"{sim.host_ip}"),
        ("CODEC", str(sim.codec), "DEV", dev_s),
        ("FPS", f"{getattr(sim, '_play_fps', 0) or sim.fps}fps", "MODEL", model_s),
        ("BR", br_s, "RTSP", rtsp_s),
        ("CH", "101", "MAC", str(sim.mac)),
        ("HTTP", str(sim.http_port), "SN", str(sim.serial)[:12]),
    ]
    lh = int(17 * scale)
    gap = int(6 * scale)                       # 列间距(小)
    pad = int(8 * scale)
    lab_w_l = int(max(d.textlength(r[0], font=mono_s) for r in rows) + 4 * scale)
    lab_w_r = int(max(d.textlength(r[2], font=mono_s) for r in rows) + 4 * scale)
    col_w = [0, 0]
    for r in rows:
        for ci, vi in ((0, r[1]), (1, r[3])):
            fi = _hud_val_font(vi, mono_s, cjk)
            col_w[ci] = max(col_w[ci], int(d.textlength(vi, font=fi)) + 3)  # +3 渲染余量
    pw = int(lab_w_l + col_w[0] + gap + lab_w_r + col_w[1] + 2 * pad)
    pw = min(pw, int(w * 0.92))
    pady = int(122 * scale)                    # 面板更贴底(2026-09-01 用户要求)
    px, py = int(12 * scale), h - pady
    ph = h - int(6 * scale) - py
    # 半透明面板: alpha overlay 合成到背景
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rectangle([px, py, px + pw, h - int(6 * scale)], fill=(8, 12, 24, 110))
    od.rectangle([px, py, px + pw, h - int(6 * scale)], outline=(60, 150, 220, 200))
    img = Image.alpha_composite(img, ov)
    d2 = ImageDraw.Draw(img)
    for i, (k1, v1, k2, v2) in enumerate(rows):
        y = py + int(6 * scale) + i * lh
        # 左列
        x = px + pad
        d2.text((x, y), k1, fill=(90, 170, 220), font=mono_s)
        xv = x + lab_w_l
        f1 = _hud_val_font(v1, mono_s, cjk)
        v = v1
        while v and d2.textlength(v, font=f1) > col_w[0] - 2:
            v = v[:-1]
        d2.text((xv, y), v, fill=(210, 235, 255), font=f1)
        # 右列
        x = px + pad + lab_w_l + col_w[0] + gap
        d2.text((x, y), k2, fill=(90, 170, 220), font=mono_s)
        xv = x + lab_w_r
        f2 = _hud_val_font(v2, mono_s, cjk)
        v = v2
        while v and d2.textlength(v, font=f2) > col_w[1] - 2:
            v = v[:-1]
        d2.text((xv, y), v, fill=(210, 235, 255), font=f2)
    # 右下特效标签(半透明)
    tag = f"FX {sim._hud_theme}"
    tw = d2.textlength(tag, font=mono_s)
    ov2 = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od2 = ImageDraw.Draw(ov2)
    od2.rectangle([w - tw - int(16 * scale), h - int(24 * scale), w - int(4 * scale), h - int(6 * scale)],
                  fill=(8, 12, 24, 110))
    img = Image.alpha_composite(img, ov2)
    d3 = ImageDraw.Draw(img)
    d3.text((w - tw - int(10 * scale), h - int(22 * scale)), tag, fill=(110, 200, 230), font=mono_s)
    return img, mono_m, mono_s


def _hud_render_static(sim):
    """一次性渲染: 主题背景 + 信息层(半透明面板) → RGB Image(缓存)."""
    from PIL import Image, ImageDraw
    w, h = sim.video_width, sim.video_height
    scale = max(0.65, min(2.5, min(w / 640.0, h / 360.0)))
    rng = random.Random(hashlib.md5((sim.mac or sim.serial).encode("utf-8")).digest())
    static_fn, _anim = _HUD_THEMES[sim._hud_theme]
    img = static_fn(w, h, rng).convert("RGBA")
    d = ImageDraw.Draw(img)
    img, _ml, _ms = _hud_draw_info(img, d, sim, w, h, scale)
    return img.convert("RGB")


def _hud_render_frame(sim):
    """每帧合成: 静态层副本 + 主题动画 + 时间戳/帧计数 → JPEG bytes."""
    try:
        from PIL import Image, ImageDraw
        if sim._hud_base is None:
            sim._hud_base = _hud_render_static(sim)
        frame = sim._hud_frame
        sim._hud_frame += 1
        img = sim._hud_base.copy()
        w, h = img.size
        scale = max(0.65, min(2.5, min(w / 640.0, h / 360.0)))
        _anim = _HUD_THEMES[sim._hud_theme][1]
        d = ImageDraw.Draw(img, "RGBA")
        _anim(d, frame, w, h, random.Random((sim.mac or sim.serial) + str(frame)))
        # 时间戳(标题栏下方右上, 秒级缓存) + FRAME 计数(竖排, 不碰 LIVE 徽标)
        mono_l = _hud_font(scale, mono=True, size=18)
        mono_s = _hud_font(scale, mono=True, size=11)
        ts = time.strftime("%H:%M:%S")
        if ts != sim._hud_ts:
            sim._hud_ts = ts
            date_s = time.strftime("%Y-%m-%d")
            tmp = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            tdd = ImageDraw.Draw(tmp)
            tw1 = int(tdd.textlength(ts, font=mono_l)) + 2
            tw2 = int(tdd.textlength(date_s, font=mono_s)) + 2
            t = Image.new("RGBA", (max(tw1, tw2), int(46 * scale)), (0, 0, 0, 0))
            td = ImageDraw.Draw(t)
            td.text((0, 0), ts, fill=(160, 240, 255, 255), font=mono_l)
            td.text((0, int(24 * scale)), date_s, fill=(120, 170, 200, 220), font=mono_s)
            sim._hud_ts_img = t
        ts_y = int(40 * scale)
        img.paste(sim._hud_ts_img, (w - sim._hud_ts_img.width - int(10 * scale), ts_y),
                  sim._hud_ts_img)
        d = ImageDraw.Draw(img)
        d.text((w - int(150 * scale), ts_y + int(48 * scale)), f"FRAME {frame % 1000000:06d}",
               fill=(110, 150, 190), font=mono_s)
        buf = __import__("io").BytesIO()
        q = 85 if max(w, h) <= 1280 else 72
        img.save(buf, format="JPEG", quality=q)
        return buf.getvalue()
    except Exception:
        return _gen_snapshot_jpeg(f"{sim.device_name}  {sim.model}", sim.video_width, sim.video_height)


# ── 摄像头模拟器 ───────────────────────────────────────────────
class OnvifCamSimulator:
    """ONVIF 摄像头模拟器: SOAP(HTTP) + WS-Discovery(UDP) + RTSP(TCP)."""

    def __init__(
        self,
        host_ip: str,
        http_port: int = 8000,
        rtsp_port: int = 8554,
        username: str = "admin",
        password: str = "12345",
        model: str = "Tingtao-Sim-Cam-1",
        manufacturer: str = "Tingtao",
        serial: str = "",
        device_name: str = "Tingtao Sim Camera",
        mac: str = "",
        fault: Optional[Dict[str, Any]] = None,
        # 媒体源与编码参数 (2026-09-01 新增: 播放指定图片/视频 + 分辨率/码率/编码可设)
        media_source: Optional[str] = None,   # 图片(jpg/png/bmp) / 视频(mp4/avi/mkv...) / .h264 裸流文件
        width: int = 1920,                    # 视频分辨率宽 (ONVIF profile + 转码目标)
        height: int = 1080,                   # 视频分辨率高
        bitrate_kbps: int = 4096,             # 码率 kbps
        fps: int = 25,                        # 帧率
        codec: str = "H264",                  # H264 / H265 / MJPEG
    ):
        self.host_ip = host_ip
        self.http_port = http_port
        self.rtsp_port = rtsp_port
        self.username = username
        self.password = password
        self.model = model
        self.manufacturer = manufacturer
        self.serial = serial or ("SIM" + uuid.uuid4().hex[:8].upper())
        self.device_name = device_name
        self.mac = mac or self._gen_mac()
        self.uid = "uuid:" + str(uuid.uuid4())
        self.fault = dict(fault or {})
        # 媒体源/编码配置 (2026-09-01)
        self.media_source = media_source
        self.video_width = max(64, int(width or 1920))
        self.video_height = max(64, int(height or 1080))
        self.bitrate_kbps = max(16, int(bitrate_kbps or 4096))
        self.fps = max(1, min(60, int(fps or 25)))
        # 子码流参数 (2026-09-01: 主/子码流双流, 子流=半分辨率+低帧率)
        self.sub_width = max(64, self.video_width // 2)
        self.sub_height = max(64, self.video_height // 2)
        self.sub_fps = max(1, min(15, self.fps // 3))
        codec = (codec or "H264").upper().replace("H.26", "H26")
        if codec not in ("H264", "H265", "MJPEG"):
            codec = "H264"
        self.codec = codec
        self._play_frames: List[bytes] = []      # 播放帧序列(Annex-B), 优先于内置彩条流
        self._play_keys: List[bool] = []
        self._play_fps: int = 25
        self._media_note: str = ""               # 媒体源准备结果说明(降级/转码信息)
        # 服务端点
        self.xaddr = f"http://{host_ip}:{http_port}/onvif/device_service"
        self.media_xaddr = f"http://{host_ip}:{http_port}/onvif/media_service"
        self.ptz_xaddr = f"http://{host_ip}:{http_port}/onvif/ptz_service"
        self.event_xaddr = f"http://{host_ip}:{http_port}/onvif/event_service"
        self.imaging_xaddr = f"http://{host_ip}:{http_port}/onvif/imaging_service"
        self.rtsp_url = f"rtsp://{host_ip}:{rtsp_port}/Streaming/Channels/101"
        self.snapshot_url = f"http://{host_ip}:{http_port}/onvif/snapshot.jpg"
        # 运行时
        self.http_server: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._wsd_sock: Optional[socket.socket] = None
        self._wsd_thread: Optional[threading.Thread] = None
        self._rtsp_sock: Optional[socket.socket] = None
        self._rtsp_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rtsp_sessions: Dict[str, Dict[str, Any]] = {}
        self._rtsp_seq = 0
        # 2026-08-19 修复(DEBUG/ONVIF 报告: 模拟摄像头黑屏): 内置真实 H.264 测试流,
        # PLAY 后循环发送 RTP 帧——录像机可真正解码出画面(彩条), 不再只有信令无数据。
        self._h264_frames: List[bytes] = []
        self._h264_keys: List[bool] = []
        self._h264_ssrc = random.randint(1, 0x7FFFFFFF)  # RTP SSRC
        try:
            # 2026-08-19: 相对导入(tools 包内), PyInstaller 静态分析可收集;
            # hiddenimports 兜底加 'h264_sim_data'。不用绝对导入(打包后模块路径不稳)。
            from .h264_sim_data import H264_FRAMES, H264_FPS as _fps
            self._h264_fps = _fps
            self._h264_frames = [base64.b64decode(fb) for fb, _k in H264_FRAMES]
            self._h264_keys = [k for _fb, k in H264_FRAMES]
        except Exception as _e:
            self._h264_frames = []
            self._h264_fps = 25
        self.lock = threading.Lock()
        self.running = False
        # 动态 HUD 快照状态 (2026-09-01: 8 主题特效 + 身份信息面板)
        self._hud_theme = _hud_pick_theme(self)
        self._hud_base: Any = None          # 静态层(背景+信息面板)缓存
        self._hud_frame = 0                 # 动画帧计数
        self._hud_ts = ""                   # 秒级时间戳缓存
        self._hud_ts_img: Any = None
        self._wm_jpeg: Optional[bytes] = None   # 图片源水印图缓存
        self.started_at = 0.0
        self.start_error: Optional[str] = None
        self.http_up = False
        self.wsd_up = False
        self.rtsp_up = False
        # 请求日志(供 cam_logs 查看)
        self.request_log: List[Dict[str, Any]] = []
        self.log_lock = threading.Lock()
        self.counters: Dict[str, int] = {}
        self.pullpoint_msgs: List[Dict[str, Any]] = []
        self.pullpoint_seq = 1

    # ── 媒体源准备 (2026-09-01: 图片/视频/.h264 播放 + 分辨率/码率/编码) ──
    @staticmethod
    def _ffmpeg_path() -> Optional[str]:
        """找 ffmpeg: PATH 优先, PyInstaller 打包(frozen)时找可执行文件同目录。"""
        try:
            import shutil
            p = shutil.which("ffmpeg")
            if p:
                return p
            if getattr(sys, "frozen", False):
                base = os.path.dirname(sys.executable)
                for cand in (os.path.join(base, "ffmpeg.exe"), os.path.join(base, "ffmpeg")):
                    if os.path.exists(cand):
                        return cand
        except Exception:
            pass
        return None

    @staticmethod
    def _slice_first_mb(nal: bytes) -> Optional[int]:
        """解析 slice NAL 的 first_mb_in_slice (Exp-Golomb ue)。失败返回 None。

        一帧(AU)的多个 slice 中, 首个 slice 的 first_mb_in_slice == 0;
        用于区分"同帧后续 slice"与"新帧"(多 slice 编码的流, 如 libx264 多线程)。"""
        if not nal:
            return None
        t = nal[0] & 0x1F
        if t not in (1, 2, 3, 4, 5):
            return None
        bits = []
        for b in nal[1:]:
            for k in range(7, -1, -1):
                bits.append((b >> k) & 1)
            if len(bits) > 64:
                break
        i = 0
        zeros = 0
        while i < len(bits) and bits[i] == 0:
            zeros += 1
            i += 1
        if i >= len(bits) or zeros > 20:
            return None
        val = 1
        for k in range(zeros):
            if i + k >= len(bits):
                return None
            val = (val << 1) | bits[i + k]
        return val - 1

    @staticmethod
    def _parse_h264_file(path: str):
        """解析 Annex-B .h264 裸流 → (frames: List[bytes], keys: List[bool])。

        NAL 按起始码(00 00 00 01 / 00 00 01)切分; 帧(AU)边界 = VCL slice 的
        first_mb_in_slice==0(多 slice 流正确合成一帧); SPS/PPS/SEI 并入其后的
        第一帧; 关键帧 = 帧内含 IDR(type 5)。
        """
        with open(path, "rb") as f:
            data = f.read()
        nals: List[bytes] = []
        i, n = 0, len(data)
        while i < n - 3:
            if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
                start = i + 3
                if start < n and data[start] == 0:  # 4 字节起始码
                    start += 1
                j = start
                while j < n - 3:
                    if data[j] == 0 and data[j + 1] == 0 and (data[j + 2] == 1 or (data[j + 2] == 0 and j + 3 < n and data[j + 3] == 1)):
                        break
                    j += 1
                nals.append(data[start:j])
                i = j
            else:
                i += 1
        frames: List[bytes] = []
        keys: List[bool] = []
        cur = b""
        cur_has_vcl = False
        cur_key = False
        for nal in nals:
            if not nal:
                continue
            t = nal[0] & 0x1F
            if t in (1, 2, 3, 4, 5):  # VCL slice
                fm = OnvifCamSimulator._slice_first_mb(nal)
                new_frame = (not cur_has_vcl) or (fm == 0)
                if new_frame and cur_has_vcl:
                    frames.append(cur)
                    keys.append(cur_key)
                    cur = b""
                    cur_has_vcl = False
                    cur_key = False
                cur += b"\x00\x00\x00\x01" + nal
                cur_has_vcl = True
                cur_key = cur_key or (t == 5)
            else:
                # SPS/PPS/SEI/AUD 等并入其后第一帧
                cur += b"\x00\x00\x00\x01" + nal
        if cur and cur_has_vcl:
            frames.append(cur)
            keys.append(cur_key)
        return frames, keys

    def _prepare_media(self) -> None:
        """按 media_source/分辨率/码率/编码 准备播放帧序列。

        - media_source=.h264 → 直接解析(无需 ffmpeg)
        - media_source=图片/视频 → ffmpeg 转码为 Annex-B .h264
        - 无源 → ffmpeg testsrc2 生成指定参数测试图
        - ffmpeg 不可用 → 降级内置 640x360 彩条流(忽略参数)
        - codec=MJPEG → 不预生成帧, RTSP 发送时用快照 JPEG 循环
        """
        self._media_note = ""
        fps_target = self.fps
        src = self.media_source
        ff = self._ffmpeg_path()
        tmp_path = None
        is_img = False
        try:
            if src and os.path.isfile(src):
                ext = os.path.splitext(src)[1].lower()
                is_img = ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp")
                if ext == ".h264":
                    frames, keys = self._parse_h264_file(src)
                    if frames:
                        self._play_frames, self._play_keys = frames, keys
                        self._play_fps = fps_target
                        self._media_note = f"h264裸流: {os.path.basename(src)} ({len(frames)}帧)"
                        return
                    self._media_note = "h264 文件无可解析帧"
                elif ff:
                    fd, tmp_path = tempfile.mkstemp(suffix=".h264")
                    os.close(fd)
                    enc = "libx265" if self.codec == "H265" else "libx264"
                    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"]
                    if is_img:
                        cmd += ["-loop", "1", "-i", src]
                    else:
                        cmd += ["-i", src]
                    cmd += ["-an", "-c:v", enc, "-pix_fmt", "yuv420p",
                            "-s", f"{self.video_width}x{self.video_height}",
                            "-r", str(fps_target), "-b:v", f"{self.bitrate_kbps}k",
                            "-preset", "ultrafast"]
                    # 单 slice 输出(多线程默认多 slice, 解析/推流按 AU 帧更稳)
                    cmd += ["-x264-params", "slices=1"] if enc == "libx264" else ["-x265-params", "slices=1"]
                    if is_img:
                        cmd += ["-tune", "stillimage", "-t", "60"]
                    else:
                        cmd += ["-tune", "zerolatency"]
                    cmd += ["-f", "h264", tmp_path]
                    subprocess.run(cmd, timeout=90, capture_output=True)
                    frames, keys = self._parse_h264_file(tmp_path)
                    if frames:
                        self._play_frames, self._play_keys = frames, keys
                        self._play_fps = fps_target
                        self._media_note = (
                            f"{'图片' if is_img else '视频'}: {os.path.basename(src)} "
                            f"→ {self.video_width}x{self.video_height} {self.codec} {self.bitrate_kbps}kbps"
                        )
                        return
                    self._media_note = f"ffmpeg 转码失败: {os.path.basename(src)}"
                else:
                    self._media_note = "无 ffmpeg, 图片/视频源不可用(仅支持 .h264)"
            elif ff and self.codec != "MJPEG":
                fd, tmp_path = tempfile.mkstemp(suffix=".h264")
                os.close(fd)
                enc = "libx265" if self.codec == "H265" else "libx264"
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
                       "-f", "lavfi", "-i",
                       f"testsrc2=size={self.video_width}x{self.video_height}:rate={fps_target}",
                       "-t", "30", "-c:v", enc, "-pix_fmt", "yuv420p",
                       "-b:v", f"{self.bitrate_kbps}k", "-preset", "ultrafast"]
                # 单 slice 输出(多线程默认多 slice, 解析/推流按 AU 帧更稳)
                cmd += ["-x264-params", "slices=1"] if enc == "libx264" else ["-x265-params", "slices=1"]
                cmd += ["-f", "h264", tmp_path]
                subprocess.run(cmd, timeout=90, capture_output=True)
                frames, keys = self._parse_h264_file(tmp_path)
                if frames:
                    self._play_frames, self._play_keys = frames, keys
                    self._play_fps = fps_target
                    self._media_note = (
                        f"测试图 {self.video_width}x{self.video_height} {self.codec} {self.bitrate_kbps}kbps"
                    )
                    return
            # 降级: 内置彩条流(忽略宽高/码率)
            self._media_note = "媒体源不可用, 降级内置 640x360 彩条流"
        except Exception as e:
            self._media_note = f"媒体准备失败: {e} (降级内置流)"
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _snapshot_bytes(self) -> bytes:
        """快照 JPEG: 图片源 → 水印图(缓存); 否则动态 HUD 特效帧(2026-09-01)."""
        src = self.media_source
        if src and os.path.isfile(src):
            ext = os.path.splitext(src)[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"):
                try:
                    from PIL import Image, ImageDraw
                    if self._wm_jpeg is None:
                        img = Image.open(src).convert("RGB")
                        img = img.resize((self.video_width, self.video_height))
                        # 右下角水印: IP + 设备名 + 分辨率/编码(区分模拟摄像头)
                        d = ImageDraw.Draw(img)
                        mono = _hud_font(0.8, mono=True, size=11)
                        lines = [
                            f"{self.host_ip}  {self.device_name}",
                            f"{self.video_width}x{self.video_height} {self.codec} {getattr(self, '_play_fps', 0) or self.fps}fps  SIM-CAM",
                        ]
                        th = int(26)
                        d.rectangle([0, img.height - th, img.width, img.height], fill=(8, 10, 18))
                        d.text((8, img.height - th + 3), lines[0], fill=(180, 230, 255), font=mono)
                        d.text((8, img.height - th + 14), lines[1], fill=(120, 170, 200), font=mono)
                        buf = __import__("io").BytesIO()
                        img.save(buf, format="JPEG", quality=85)
                        self._wm_jpeg = buf.getvalue()
                    return self._wm_jpeg
                except Exception:
                    try:
                        with open(src, "rb") as f:
                            return f.read()
                    except Exception:
                        pass
        return _hud_render_frame(self)

    # ── 工具 ──
    def _sub_resize(self, jpeg: bytes, w: int, h: int) -> bytes:
        """子码流降采样: 主帧 JPEG → 子分辨率 JPEG (Pillow, 2026-09-01)."""
        try:
            from PIL import Image
            img = Image.open(__import__("io").BytesIO(jpeg)).convert("RGB")
            img = img.resize((w, h))
            buf = __import__("io").BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
        except Exception:
            return jpeg

    def _extract_jpeg_entropy(self, jpeg: bytes):
        """RFC2435 封装用: 从 JPEG 文件提取 (熵数据, 8bit量化表[2×64B])。

        熵数据 = SOS 段之后的所有字节(纯熵编码, 含 RST 标记);
        量化表 = DQT 段提取 8-bit 表(16-bit 表取低字节), 最多 128B。
        ffmpeg rtpdec_jpeg.c 自己生成 SOI/APP0/DQT/DHT/SOF0/SOS/EOI,
        所以 payload 必须只含熵数据, 否则双段损坏。
        """
        n = len(jpeg)
        i = 0
        qtables = bytearray()
        entropy = b""
        while i < n - 3:
            if jpeg[i] != 0xFF:
                i += 1
                continue
            m = jpeg[i + 1]
            if m == 0x00:            # 填充字节
                i += 2
                continue
            if m in (0xD8, 0x01):    # SOI / TEM(无长度)
                i += 2
                continue
            if m == 0xDA:            # SOS → 其后是熵数据
                seg_len = (jpeg[i + 2] << 8) | jpeg[i + 3]
                entropy = jpeg[i + 2 + seg_len:]
                break
            seg_len = (jpeg[i + 2] << 8) | jpeg[i + 3]
            if m == 0xDB and seg_len > 2 and len(qtables) < 128:
                p = i + 4
                end = i + 2 + seg_len
                while p + 1 < end and len(qtables) < 128:
                    pq = jpeg[p]
                    p += 1
                    if pq >> 4:      # 16-bit 精度: 64×2B, 取低字节
                        for k in range(64):
                            if p + 2 * k + 1 < end:
                                hi, lo = jpeg[p + 2 * k], jpeg[p + 2 * k + 1]
                                qtables.append(lo if lo else hi)
                        p += 128
                    else:            # 8-bit 精度: 64B
                        qtables.extend(jpeg[p:p + 64])
                        p += 64
            i += 2 + seg_len
        return entropy, bytes(qtables[:128])

    def _gen_mac(self) -> str:
        return "02:00:00:%02x:%02x:%02x" % (os.getpid() & 0xFF, int(time.time()) & 0xFF, uuid.uuid4().bytes[0])

    def _log(self, kind: str, detail: str, ok: bool = True, extra: Any = None) -> None:
        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "kind": kind,  # soap / wsdiscovery / rtsp / snapshot / auth
            "detail": detail,
            "ok": ok,
        }
        if extra is not None:
            entry["extra"] = extra
        with self.log_lock:
            self.request_log.append(entry)
            if len(self.request_log) > 500:
                self.request_log = self.request_log[-500:]
            self.counters[kind] = self.counters.get(kind, 0) + 1
        logger.info("[%s] %s: %s", kind, "OK " if ok else "ERR", detail)

    def _check_http_digest(self, auth_header: str, method: str, uri: str, body: bytes) -> bool:
        """HTTP Digest 认证校验(MD5, RFC 2617)。真实录像机(如大华)添加摄像头时用 Digest."""
        try:
            # Authorization: Digest username="admin", realm="...", nonce="...", uri="...", response="...", qop=auth, nc=..., cnonce=...
            params = {}
            for part in auth_header[len("Digest "):].split(","):
                k, _, v = part.strip().partition("=")
                params[k.strip()] = v.strip().strip('"')
            username = params.get("username", "")
            realm = params.get("realm", "")
            nonce = params.get("nonce", "")
            uri = params.get("uri", uri)
            response = params.get("response", "")
            qop = params.get("qop", "")
            nc = params.get("nc", "")
            cnonce = params.get("cnonce", "")
            if username != self.username:
                return False
            ha1 = hashlib.md5(f"{username}:{realm}:{self.password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if qop:
                expected = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
            else:
                expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return expected == response
        except Exception:
            return False

    def _check_auth(self, headers: Dict[str, str], body: bytes) -> Optional[bytes]:
        """认证检查. 返回 None=通过, bytes=需要返回的错误响应/401."""
        if self.fault.get("wrong_password"):
            return b"__401__"
        # WS-UsernameToken (SOAP Header)
        try:
            if body and b"UsernameToken" in body[:4096]:
                root = ET.fromstring(body)
                token = root.find(f".//{_q('UsernameToken', NS['wsse'])}")
                if token is not None:
                    user_el = token.find(_q("Username", NS["wsse"]))
                    nonce_el = token.find(_q("Nonce", NS["wsse"]))
                    created_el = token.find(_q("Created", NS["wsu"]))
                    digest_el = token.find(_q("Password", NS["wsse"]))
                    if user_el is not None and user_el.text == self.username:
                        if digest_el is not None and "Digest" in (digest_el.get("Type") or ""):
                            nonce_b64 = nonce_el.text if nonce_el is not None else ""
                            created = created_el.text if created_el is not None else ""
                            try:
                                nonce_bin = base64.b64decode(nonce_b64)
                            except Exception:
                                nonce_bin = nonce_b64.encode()
                            digest = base64.b64encode(
                                hashlib.sha1(nonce_bin + created.encode() + self.password.encode()).digest()
                            ).decode()
                            if digest == digest_el.text:
                                return None
                        elif digest_el is not None and digest_el.text == self.password:
                            return None  # 明文 PasswordText
        except Exception:
            pass
        return b"__401__"

    # ── 生命周期 ──
    def start(self) -> None:
        self._stop.clear()
        self.running = True
        self.started_at = time.time()
        self.start_error = None
        # 2026-09-01: 准备播放媒体源(图片/视频/.h264 或按分辨率/码率/编码生成测试流)
        self._prepare_media()
        self._log("soap", f"媒体源: {self._media_note or '(内置彩条流)'}")
        # HTTP SOAP 服务 (绑定失败必须回传, 不能静默吞掉)
        try:
            handler = self._make_handler()
            self.http_server = ThreadingHTTPServer(("0.0.0.0", self.http_port), handler)
        except OSError as e:
            self.running = False
            self.start_error = f"HTTP 端口 {self.http_port} 绑定失败: {e}"
            logger.error(self.start_error)
            return
        self._http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True, name="onvif-http")
        self._http_thread.start()
        self.http_up = True
        # WS-Discovery
        if not self.fault.get("disable_discovery"):
            try:
                self._start_wsd()
                self.wsd_up = True
            except OSError as e:
                logger.warning("WS-Discovery 启动失败(继续): %s", e)
                self.start_error = f"WS-Discovery 启动失败: {e}"
        else:
            self._log("wsdiscovery", "disabled by fault inject", ok=False)
        # RTSP
        if not self.fault.get("disable_rtsp"):
            try:
                self._start_rtsp()
                self.rtsp_up = True
            except OSError as e:
                logger.warning("RTSP 启动失败(继续): %s", e)
                self.start_error = (self.start_error or "") + f" RTSP 启动失败: {e}"
        else:
            self._log("rtsp", "disabled by fault inject", ok=False)
        self._log("soap", f"ONVIF 模拟摄像头启动: {self.xaddr} (user={self.username})")

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self.http_server:
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
            except Exception:
                pass
            self.http_server = None
        if self._wsd_sock:
            try:
                self._wsd_sock.close()
            except Exception:
                pass
            self._wsd_sock = None
        if self._rtsp_sock:
            try:
                self._rtsp_sock.close()
            except Exception:
                pass
            self._rtsp_sock = None

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "http_up": self.http_up,
            "wsd_up": self.wsd_up,
            "rtsp_up": self.rtsp_up,
            "start_error": self.start_error,
            "xaddr": self.xaddr,
            "media_xaddr": self.media_xaddr,
            "rtsp_url": self.rtsp_url,
            "snapshot_url": self.snapshot_url,
            "username": self.username,
            "model": self.model,
            "serial": self.serial,
            "mac": self.mac,
            "uid": self.uid,
            "fault": self.fault,
            "codec": self.codec,
            "resolution": f"{self.video_width}x{self.video_height}",
            "bitrate_kbps": self.bitrate_kbps,
            "fps": self.fps,
            "media_source": self.media_source,
            "media_note": self._media_note,
            "uptime_s": int(time.time() - self.started_at) if self.started_at else 0,
            "counters": dict(self.counters),
            "log_len": len(self.request_log),
        }

    # ── WS-Discovery (UDP 3702) ──
    def _start_wsd(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT 是 Linux 专属, Windows 无此属性(AttributeError)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", 3702))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton("239.255.255.250") + socket.inet_aton(self.host_ip))
        except Exception as e:  # Windows 接口/权限问题也可能抛非 OSError
            logger.warning("WS-Discovery 组播加入失败(单播仍可用): %s", e)
        self._wsd_sock = sock
        self._wsd_thread = threading.Thread(target=self._wsd_loop, daemon=True, name="onvif-wsd")
        self._wsd_thread.start()

    def _wsd_loop(self) -> None:
        sock = self._wsd_sock
        while not self._stop.is_set():
            try:
                sock.settimeout(1.0)
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = self._handle_probe(data, addr)
                if reply:
                    sock.sendto(reply, addr)
            except Exception as e:
                logger.warning("WSD 处理异常: %s", e)

    def _handle_probe(self, data: bytes, addr) -> Optional[bytes]:
        try:
            root = ET.fromstring(data)
        except Exception:
            return None
        # Probe 在 Body 内, 直接找 d:Probe 元素
        probe_el = root.find(f".//{{{NS['d']}}}Probe")
        if probe_el is None:
            return None
        types_el = probe_el.find(f"{{{NS['d']}}}Types")
        want_video = True
        if types_el is not None and types_el.text:
            types = types_el.text
            want_video = ("NetworkVideoTransmitter" in types or "NetworkVideoDisplay" in types
                          or "Device" in types or "NetworkVideoReceiver" in types
                          or "NetworkVideoStorage" in types)
        self._log("wsdiscovery", f"Probe from {addr[0]}:{addr[1]} types={types_el.text if types_el is not None else '(any)'}")
        if not want_video:
            return None
        message_id = str(uuid.uuid4())
        # 注意: 响应必须 <1500 字节(WSL loopback 与多数真实网络丢大 UDP 包/组播分片)。
        # 只声明必要命名空间, Scopes 精简。
        scopes = (
            f"onvif://www.onvif.org/name/{self.device_name} "
            f"onvif://www.onvif.org/type/video_encoder "
            f"onvif://www.onvif.org/hardware/{self.model} "
            f"onvif://www.onvif.org/mac/{self.mac}"
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:wsa="http://www.w3.org/2005/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
            f'<soap:Header><wsa:MessageID>{message_id}</wsa:MessageID>'
            '<wsa:RelatesTo>urn:uuid:0</wsa:RelatesTo></soap:Header>'
            '<soap:Body><d:ProbeMatches><d:ProbeMatch>'
            f'<d:EndpointReference><wsa:Address>{self.uid}</wsa:Address></d:EndpointReference>'
            '<d:Types>dn:NetworkVideoTransmitter</d:Types>'
            f'<d:Scopes>{scopes}</d:Scopes>'
            f'<d:XAddrs>{self.xaddr}</d:XAddrs>'
            '<d:MetadataVersion>1</d:MetadataVersion>'
            '</d:ProbeMatch></d:ProbeMatches></soap:Body></soap:Envelope>'
        ).encode()
        return xml

    # ── RTSP ──
    def _start_rtsp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.rtsp_port))
        sock.listen(8)
        sock.settimeout(1.0)
        self._rtsp_sock = sock
        self._rtsp_thread = threading.Thread(target=self._rtsp_loop, daemon=True, name="onvif-rtsp")
        self._rtsp_thread.start()

    def _rtsp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._rtsp_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._rtsp_conn, args=(conn, addr), daemon=True, name="onvif-rtsp-conn")
            t.start()

    def _rtsp_conn(self, conn: socket.socket, addr) -> None:
        conn.settimeout(10)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\r\n\r\n" in buf:
                    head, _, buf = buf.partition(b"\r\n\r\n")
                    lines = head.decode("utf-8", "replace").split("\r\n")
                    if not lines:
                        continue
                    method, uri, ver = lines[0].split(" ", 2)
                    hdrs = {}
                    for ln in lines[1:]:
                        if ":" in ln:
                            k, v = ln.split(":", 1)
                            hdrs[k.strip().lower()] = v.strip()
                    reply = self._rtsp_handle(method, uri, hdrs, addr[0])
                    if reply is None:
                        conn.close()
                        return
                    conn.sendall(reply)
                    if method == "TEARDOWN":
                        conn.close()
                        return
                    if method == "PLAY":
                        # 2026-08-19 修复(DEBUG/ONVIF 报告: 模拟摄像头黑屏): PLAY 后
                        # 在同一 TCP 连接上用 RTP/TCP interleaved 通道(0)发送真实
                        # 帧流——录像机/播放器可真正解码出画面。阻塞循环发送,
                        # 连接断开或收到 TEARDOWN 时退出。2026-09-01: 按流类型(主/子)。
                        sess = hdrs.get("session", "")
                        st = self._rtsp_sessions.get(sess, {}).get("stream", "main")
                        self._rtsp_send_frames(conn, st)
                        return
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _rtsp_send_frames(self, conn: socket.socket, stream: str = "main") -> None:
        """RTP/TCP-interleaved 发送 H.264 帧流(2026-08-19 新增, 2026-09-01 支持自定义媒体源+主子码流)。

        - 帧数据为 Annex-B(00 00 00 01 分隔的 NAL); 每帧拆 NAL, 大 NAL 用
          FU-A 分片(≤1400B); 每帧末尾 RTP M 位置 1。
        - fault.no_video=True 时只发 SPS/PPS 不发帧数据(模拟黑屏/信令正常但无画面)。
        - 帧源: _play_frames(media_source/分辨率码率编码生成) 优先, 否则内置彩条流。
        - codec=MJPEG 时走 _rtsp_send_mjpeg (RTP payload 26)。
        - stream=sub: 子码流(降帧率发送, MJPEG 走子分辨率)。
        """
        if self.codec == "MJPEG":
            self._rtsp_send_mjpeg(conn, stream)
            return
        if self.codec == "H265":
            self._rtsp_send_hevc_frames(conn, stream)
            return
        frames = self._play_frames if self._play_frames else self._h264_frames
        keys = self._play_keys if self._play_frames else self._h264_keys
        if not frames:
            self._log("rtsp", "PLAY: 无可用 H.264 帧数据, 跳过视频发送", ok=False)
            return
        ssrc = self._h264_ssrc
        seq = random.randint(0, 0xFFFF)
        base_fps = max(1, self._play_fps if self._play_frames else (self._h264_fps or 25))
        fps = self.sub_fps if stream == "sub" else base_fps
        frame_interval = 1.0 / fps
        ts_step = int(90000 / fps)
        ts = 0
        idx = 0
        no_video = bool(self.fault.get("no_video") or self.fault.get("black_screen"))
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                fb64, is_key = frames[idx % len(frames)], keys[idx % len(frames)]
                frame = base64.b64decode(fb64) if isinstance(fb64, str) else fb64
                # 解析 Annex-B NAL
                nals = []
                i = 0
                n = len(frame)
                while i < n - 3:
                    if frame[i] == 0 and frame[i+1] == 0 and frame[i+2] == 1:
                        start = i + 3
                        j = start
                        while j < n - 3:
                            if frame[j] == 0 and frame[j+1] == 0 and (frame[j+2] == 1 or (frame[j+2] == 0 and j+3 < n and frame[j+3] == 1)):
                                break
                            j += 1
                        nals.append(frame[start:j])
                        i = j
                    else:
                        i += 1
                if no_video:
                    # 黑屏故障注入: 只发参数集(关键帧的 SPS/PPS), 不发 VCL 数据
                    nals = [x for x in nals if (x[0] & 0x1F) in (7, 8)]
                for ni, nal in enumerate(nals):
                    nal_type = nal[0] & 0x1F
                    # 关键帧首 NAL 前的 SPS/PPS 每次都发(保证新会话可解码)
                    if len(nals) > 1 and ni == 0 and is_key:
                        pass  # SPS/PPS 随帧发送
                    last = (ni == len(nals) - 1)
                    self._rtsp_send_nal(conn, nal, nal_type, seq, ts, ssrc, marker=last)
                    seq = (seq + 1) & 0xFFFF
                ts = (ts + ts_step) & 0xFFFFFFFF
                idx += 1
                # 帧节流: 保持 25fps
                elapsed = time.monotonic() - t0
                sleep_t = frame_interval - elapsed
                if sleep_t > 0:
                    # 用短睡以便响应 stop/连接断开
                    end = time.monotonic() + sleep_t
                    while time.monotonic() < end and not self._stop.is_set():
                        time.sleep(0.005)
        except (OSError, ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            self._log("rtsp", f"视频发送线程异常: {type(e).__name__}: {e}", ok=False)

    def _rtsp_send_nal(self, conn: socket.socket, nal: bytes, nal_type: int,
                       seq: int, ts: int, ssrc: int, marker: bool) -> None:
        """单个 NAL 经 RTP/TCP-interleaved 发送(≤1400B 单包, 大 NAL 用 FU-A 分片)。"""
        MAX_PAYLOAD = 1200  # 保守 MTU, 留 TCP 头空间
        # 12 字节 RTP 头 + 2 字节通道帧头($ + channel)
        # 2026-09-01 修复: marker 位必须 0x80 (原 96|1=97 把 payload type 改 97, ffprobe 不认帧边界)
        if len(nal) <= MAX_PAYLOAD:
            rtp = struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | 96, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
            pkt = b"\x24\x00" + struct.pack(">H", len(rtp) + len(nal)) + rtp + nal
            conn.sendall(pkt)
            return
        # FU-A 分片: 1 字节 FU indicator + 1 字节 FU header, 每片 ≤ MAX_PAYLOAD
        fu_indicator = (nal[0] & 0xE0) | 28  # 保留原 NAL 头前 3 位, type=28 (FU-A)
        payload = nal[1:]  # 去掉 NAL header
        total = len(payload)
        off = 0
        # 提前算分片数: 每片 payload ≤ MAX_PAYLOAD - 2
        chunk_size = MAX_PAYLOAD - 2
        first = True
        while off < total:
            chunk = payload[off:off + chunk_size]
            off += chunk_size
            s = 1 if first else 0
            e = 1 if off >= total else 0
            fu_header = (s << 7) | (e << 6) | (nal_type & 0x1F)
            m = 1 if (e and marker) else 0
            rtp = struct.pack(">BBHII", 0x80, (0x80 if m else 0) | 96, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
            body = bytes([fu_indicator, fu_header]) + chunk
            pkt = b"\x24\x00" + struct.pack(">H", len(rtp) + len(body)) + rtp + body
            conn.sendall(pkt)
            first = False

    def _rtsp_send_hevc_frames(self, conn: socket.socket, stream: str = "main") -> None:
        """H.265/HEVC 帧流 (RTP payload 96, RFC7798, 2026-09-01)。

        - HEVC NAL 头 2 字节, type = (nal[0] >> 1) & 0x3F (VPS=32 SPS=33 PPS=34 IDR=19)
        - 单包: 2 字节 payload header(=NAL 头) + RBSP
        - 大 NAL: FU 分片 (payload header type=49 + 1 字节 FU header S/E/FuType)
        - stream=sub: 子码流降帧率。
        """
        frames = self._play_frames if self._play_frames else self._h264_frames
        keys = self._play_keys if self._play_frames else self._h264_keys
        if not frames:
            self._log("rtsp", "PLAY: 无可用 H.265 帧数据, 跳过视频发送", ok=False)
            return
        ssrc = random.randint(1, 0x7FFFFFFF)
        seq = random.randint(0, 0xFFFF)
        base_fps = max(1, self._play_fps if self._play_frames else (self._h264_fps or 25))
        fps = self.sub_fps if stream == "sub" else base_fps
        frame_interval = 1.0 / fps
        ts_step = int(90000 / fps)
        ts = 0
        idx = 0
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                fb64, is_key = frames[idx % len(frames)], keys[idx % len(frames)]
                frame = base64.b64decode(fb64) if isinstance(fb64, str) else fb64
                # 解析 Annex-B NAL
                nals = []
                i = 0
                n = len(frame)
                while i < n - 3:
                    if frame[i] == 0 and frame[i + 1] == 0 and frame[i + 2] == 1:
                        start = i + 3
                        if start < n and frame[start] == 0:
                            start += 1
                        j = start
                        while j < n - 3:
                            if frame[j] == 0 and frame[j + 1] == 0 and (frame[j + 2] == 1 or (frame[j + 2] == 0 and j + 3 < n and frame[j + 3] == 1)):
                                break
                            j += 1
                        nals.append(frame[start:j])
                        i = j
                    else:
                        i += 1
                for ni, nal in enumerate(nals):
                    nal_type = (nal[0] >> 1) & 0x3F if len(nal) >= 2 else 0
                    last = (ni == len(nals) - 1)
                    self._rtsp_send_hevc_nal(conn, nal, nal_type, seq, ts, ssrc, marker=last)
                    seq = (seq + 1) & 0xFFFF
                ts = (ts + ts_step) & 0xFFFFFFFF
                idx += 1
                elapsed = time.monotonic() - t0
                sleep_t = frame_interval - elapsed
                if sleep_t > 0:
                    end = time.monotonic() + sleep_t
                    while time.monotonic() < end and not self._stop.is_set():
                        time.sleep(0.005)
        except (OSError, ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            self._log("rtsp", f"H.265 发送线程异常: {type(e).__name__}: {e}", ok=False)

    def _rtsp_send_hevc_nal(self, conn: socket.socket, nal: bytes, nal_type: int,
                            seq: int, ts: int, ssrc: int, marker: bool) -> None:
        """单个 HEVC NAL 经 RTP/TCP-interleaved 发送 (≤1200 单包, 大 NAL 用 FU 分片)."""
        MAX_PAYLOAD = 1200
        if len(nal) <= MAX_PAYLOAD:
            # 单包: 2 字节 payload header = NAL 头原样
            rtp = struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | 96, seq & 0xFFFF,
                              ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
            pkt = b"\x24\x00" + struct.pack(">H", len(rtp) + len(nal)) + rtp + nal
            conn.sendall(pkt)
            return
        # FU: payload header type=49, 保持 F/LayerId/TID; 1 字节 FU header (S/E/FuType)
        fu_ind = ((nal[0] & 0x81) | (49 << 1)) << 8 | nal[1]
        payload = nal[2:]
        total = len(payload)
        off = 0
        chunk_size = MAX_PAYLOAD - 3
        first = True
        while off < total:
            chunk = payload[off:off + chunk_size]
            off += chunk_size
            s = 1 if first else 0
            e = 1 if off >= total else 0
            fu_hdr = (s << 7) | (e << 6) | (nal_type & 0x3F)
            m = 1 if (e and marker) else 0
            rtp = struct.pack(">BBHII", 0x80, (0x80 if m else 0) | 96, seq & 0xFFFF,
                              ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
            body = struct.pack(">H", fu_ind) + bytes([fu_hdr]) + chunk
            pkt = b"\x24\x00" + struct.pack(">H", len(rtp) + len(body)) + rtp + body
            conn.sendall(pkt)
            first = False

    def _rtsp_send_mjpeg(self, conn: socket.socket, stream: str = "main") -> None:
        """MJPEG 流: 快照 JPEG → RFC2435 封装发送 (RTP payload 26, 2026-09-01)。

        RFC2435 关键规则(对照 ffmpeg rtpdec_jpeg.c 源码逐条核对):
        - 每个 RTP 分片都带 8 字节 JPEG 头(ffmpeg 无条件 buf+=8);
        - fragment offset = 本片熵数据起始偏移(首片 0), ffmpeg 用它校验连续性;
        - type=1(baseline), q=255 → 首片需内嵌量化表(4B 表头 + 128B 8bit 表);
        - 数据必须是纯熵编码(ffmpeg 自建 SOI/APP0/DQT/DHT/SOF0/SOS + EOI);
        - 帧末分片 RTP M 位置 1。
        JPEG 解析/量化表提取有缓存(静态流内容不变, 避免每帧重复解析)。
        stream=sub: 子码流 = 主帧降采样到半分辨率 + 低帧率发送。
        """
        ssrc = random.randint(1, 0x7FFFFFFF)
        seq = random.randint(0, 0xFFFF)
        ts = 0
        sub = (stream == "sub")
        base_fps = max(1, self._play_fps or 25)
        fps = self.sub_fps if sub else base_fps
        interval = 1.0 / fps
        w = self.sub_width if sub else self.video_width
        h = self.sub_height if sub else self.video_height
        w_unit = min(255, max(1, (w + 7) // 8))
        h_unit = min(255, max(1, (h + 7) // 8))
        cached = None
        cached_qhdr = b""
        dynamic = not (self.media_source and os.path.isfile(self.media_source))
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                if dynamic:
                    # 动态 HUD: 每帧重生成画面, 仅缓存量化表
                    jpeg = self._snapshot_bytes()
                    if sub:
                        jpeg = self._sub_resize(jpeg, w, h)
                    entropy, qtables = self._extract_jpeg_entropy(jpeg)
                    if not entropy:
                        entropy = jpeg[2:] if jpeg[:2] == b"\xff\xd8" else jpeg
                    if not cached_qhdr:
                        cached_qhdr = struct.pack(">BBH", 0, 0, len(qtables)) + qtables
                else:
                    if cached is None:
                        jpeg = self._snapshot_bytes()
                        if sub:
                            jpeg = self._sub_resize(jpeg, w, h)
                        entropy, qtables = self._extract_jpeg_entropy(jpeg)
                        if not entropy:
                            # 兜底: 解析失败时直接用去 SOI 的原始数据(兼容非标准播放器)
                            entropy = jpeg[2:] if jpeg[:2] == b"\xff\xd8" else jpeg
                        cached = entropy
                        # 量化表头: 保留字节=0, precision=0, 表长
                        cached_qhdr = struct.pack(">BBH", 0, 0, len(qtables)) + qtables
                    entropy = cached
                MAX_DATA = 1200 - 8          # 每片扣 8B JPEG 头
                first_extra = len(cached_qhdr)
                total = len(entropy)
                off = 0
                while True:
                    is_first = (off == 0)
                    cap = MAX_DATA - (first_extra if is_first else 0)
                    chunk = entropy[off:off + cap]
                    # 8 字节 JPEG 头: type-specific=1, off(3B)=熵偏移, type=1, Q=255, w, h
                    jhdr = struct.pack(">BBHBBBB", 1, (off >> 16) & 0xFF,
                                       off & 0xFFFF, 1, 255, w_unit, h_unit)
                    payload = (jhdr + cached_qhdr + chunk) if is_first else (jhdr + chunk)
                    off += len(chunk)
                    last = off >= total
                    if getattr(self, "_dbg_mjpeg", None) is not None:
                        self._dbg_mjpeg.append((off, total, last))
                    rtp = struct.pack(">BBHII", 0x80, (0x80 if last else 0) | 26, seq & 0xFFFF,
                                      ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
                    pkt = b"\x24\x00" + struct.pack(">H", len(rtp) + len(payload)) + rtp + payload
                    conn.sendall(pkt)
                    seq = (seq + 1) & 0xFFFF
                    if last:
                        break
                ts = (ts + int(90000 / fps)) & 0xFFFFFFFF
                elapsed = time.monotonic() - t0
                sleep_t = interval - elapsed
                if sleep_t > 0:
                    end = time.monotonic() + sleep_t
                    while time.monotonic() < end and not self._stop.is_set():
                        time.sleep(0.005)
        except (OSError, ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            self._log("rtsp", f"MJPEG 发送异常: {type(e).__name__}: {e}", ok=False)

    @staticmethod
    def _stream_from_uri(uri: str) -> str:
        """RTSP URI → 流类型: /Streaming/Channels/101 主码流, /102 子码流."""
        if "102" in uri:
            return "sub"
        return "main"

    def _rtsp_handle(self, method: str, uri: str, hdrs: Dict[str, str], peer: str) -> Optional[bytes]:
        cseq = hdrs.get("cseq", "1")
        stream = self._stream_from_uri(uri)
        auth_ok = self._rtsp_auth_ok(hdrs)
        if not auth_ok and not self.fault.get("disable_rtsp_auth"):
            self._log("rtsp", f"{method} {uri} from {peer}: 401 未认证", ok=False)
            return self._rtsp_reply(401, cseq, {"WWW-Authenticate": 'Digest realm="ONVIF", nonce="sim", qop="auth"'}, "")
        if method == "OPTIONS":
            self._log("rtsp", f"OPTIONS {uri} from {peer}")
            return self._rtsp_reply(200, cseq, {"Public": "DESCRIBE, SETUP, TEARDOWN, PLAY, PAUSE, GET_PARAMETER"}, "")
        if method == "DESCRIBE":
            self._log("rtsp", f"DESCRIBE {uri} from {peer} ({stream})")
            if self.codec == "MJPEG":
                media = "m=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\n"
            elif self.codec == "H265":
                media = "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\na=fmtp:96 profile-level-id=1\r\n"
            else:
                media = "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1;profile-level-id=42C01E\r\n"
            chan = "102" if stream == "sub" else "101"
            sdp = (
                "v=0\r\n"
                f"o=- {int(time.time())} 1 IN IP4 {self.host_ip}\r\n"
                "s=Tingtao Sim Camera\r\n"
                f"c=IN IP4 {self.host_ip}\r\n"
                "t=0 0\r\n"
                f"{media}"
                "a=control:track1\r\n"
            )
            return self._rtsp_reply(200, cseq, {
                "Content-Type": "application/sdp",
                "Content-Base": f"rtsp://{self.host_ip}:{self.rtsp_port}/Streaming/Channels/{chan}/",
                "Content-Length": str(len(sdp.encode())),
            }, sdp)
        if method == "SETUP":
            self._rtsp_seq += 1
            sess = f"SIM{self._rtsp_seq:06d}"
            self._rtsp_sessions[sess] = {"created": time.time(), "stream": stream}
            self._log("rtsp", f"SETUP {uri} from {peer} → session {sess} ({stream})")
            return self._rtsp_reply(200, cseq, {
                "Session": f"{sess};timeout=60",
                "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1",
            }, "")
        if method == "PLAY":
            sess = hdrs.get("session", "")
            st = self._rtsp_sessions.get(sess, {}).get("stream", "main")
            chan = "102" if st == "sub" else "101"
            self._log("rtsp", f"PLAY {uri} from {peer} session={sess} ({st})")
            rtp = f"RTP/AVP/TCP;unicast;interleaved=0-1;ssrc=0x{self._rtsp_seq:08x}"
            reply = self._rtsp_reply(200, cseq, {
                "Session": sess,
                "Transport": rtp,
                "Range": "npt=0.000-",
                "RTP-Info": "url=rtsp://%s:%d/Streaming/Channels/%s/track1;seq=0" % (self.host_ip, self.rtsp_port, chan),
            }, "")
            return reply
        if method == "GET_PARAMETER":
            return self._rtsp_reply(200, cseq, {}, "")
        if method == "PAUSE":
            return self._rtsp_reply(200, cseq, {}, "")
        if method == "TEARDOWN":
            self._log("rtsp", f"TEARDOWN {uri} from {peer}")
            return self._rtsp_reply(200, cseq, {}, "")
        self._log("rtsp", f"{method} {uri} from {peer}: 405", ok=False)
        return self._rtsp_reply(405, cseq, {}, "")

    def _rtsp_auth_ok(self, hdrs: Dict[str, str]) -> bool:
        if self.fault.get("wrong_password"):
            return False
        auth = hdrs.get("authorization", "")
        if not auth:
            return True  # 默认不强制 RTSP 认证(与多数国产摄像头一致)
        if auth.lower().startswith("basic "):
            try:
                raw = base64.b64decode(auth.split(" ", 1)[1]).decode()
                u, _, p = raw.partition(":")
                return u == self.username and p == self.password
            except Exception:
                return False
        return True

    def _rtsp_reply(self, code: int, cseq: str, headers: Dict[str, str], body: str) -> bytes:
        reason = {200: "OK", 401: "Unauthorized", 405: "Method Not Allowed"}.get(code, "OK")
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: {cseq}"]
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        lines.append("")
        # 2026-08-19 修复(DEBUG/ONVIF 报告: 模拟摄像头拉流失败):
        # ① body 为空时不能再 append 空串——多出的空行会让客户端把 OPTIONS/SETUP
        #    响应末尾空行误读为下一个响应开头 → "CSeq N expected, 0 received";
        # ② body 字节必须与 Content-Length 严格一致: join 后整体尾部不再追加
        #    "\r\n"(body 自带结尾), 否则 Content-Length 声明 < 实际发送, 客户端
        #    按声明长度读 body 后残留字节污染下一个响应(同上 CSeq 错乱)。
        head = ("\r\n".join(lines)).encode("utf-8") + b"\r\n"
        if body:
            return head + body.encode("utf-8")
        return head

    # ── HTTP SOAP 服务 ──
    def _make_handler(self):
        sim = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, code: int, ctype: str, body: bytes, extra: Optional[Dict[str, str]] = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/onvif/snapshot.jpg", "/snapshot.jpg", "/onvif/snapshot", "/snapshot"):
                    sim._log("snapshot", f"GET {self.path} from {self.client_address[0]}")
                    body = sim._snapshot_bytes()
                    self._send(200, "image/jpeg", body)
                    return
                # 设备信息服务端点
                if path == "/onvif/device_service":
                    self._send(200, "application/soap+xml; charset=utf-8",
                               _soap_fault("GET not supported, use SOAP POST", True))
                    return
                self._send(404, "text/plain", b"Not Found")

            def do_POST(self):
                body = self._read_body()
                soap12 = "soap+xml" in (self.headers.get("Content-Type") or "").lower()
                # HTTP Digest 优先(真实录像机行为); 无则走 wsse
                auth_hdr = self.headers.get("Authorization") or ""
                if auth_hdr.lower().startswith("digest "):
                    digest_ok = sim._check_http_digest(
                        auth_hdr, "POST", self.path.split("?")[0], body)
                    auth_result = None if digest_ok else b"__401__"
                    if not digest_ok:
                        sim._log("auth", f"401 {self.path} from {self.client_address[0]} (Digest 校验失败)", ok=False)
                else:
                    auth_result = sim._check_auth(self.headers, body)
                if auth_result == b"__401__":
                    if not auth_hdr:
                        sim._log("auth", f"401 {self.path} from {self.client_address[0]} (认证失败/未提供)", ok=False)
                    realm = f'Digest realm="ONVIF", nonce="sim{int(time.time())}", qop="auth", algorithm=MD5'
                    self._send(401, "text/xml; charset=utf-8",
                               _soap_fault("Authentication required", soap12),
                               {"WWW-Authenticate": realm})
                    return
                if sim.fault.get("slow"):
                    delay = float(sim.fault.get("slow_delay") or 5)
                    time.sleep(min(delay, 30))
                resp = sim.handle_soap(self.path, body, soap12, self.client_address[0])
                self._send(200, "application/soap+xml; charset=utf-8", resp)

        return Handler

    # ── SOAP 路由 ──
    def handle_soap(self, path: str, body: bytes, soap12: bool = False, peer: str = "") -> bytes:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return _soap_fault("Malformed SOAP request", soap12)
        # 找到 Body 下第一个子元素 = 请求
        body_el = root.find(_q("Body", SOAP_NS))
        if body_el is None:
            body_el = root.find(_q("Body", SOAP12_NS))
        req = None
        if body_el is not None:
            for child in body_el:
                req = child
                break
        if req is None:
            return _soap_fault("Empty SOAP Body", soap12)
        action = _localname(req.tag)
        ns_uri = req.tag.split("}")[0].strip("{") if "}" in req.tag else ""
        # 记录请求摘要(含参数, 便于排查录像机实际发的请求)
        try:
            arg_parts = []
            for child in req:
                arg_parts.append(f"{_localname(child.tag)}={child.text or ''}")
            req_summary = f"{action}({', '.join(arg_parts)[:200]})"
        except Exception:
            req_summary = action
        self._log("soap", f"{req_summary} from {peer}")
        try:
            return self._dispatch(action, ns_uri, req, soap12)
        except Exception as e:
            logger.exception("SOAP 处理异常: %s", e)
            return _soap_fault(f"Internal error: {e}", soap12)

    def _dispatch(self, action: str, ns_uri: str, req: ET.Element, soap12: bool) -> bytes:
        # Device 服务
        if action == "GetSystemDateAndTime":
            return _soap_response(self._r_get_system_date_time(), soap12)
        if action == "GetDeviceInformation":
            return _soap_response(self._r_device_information(), soap12)
        if action == "GetCapabilities":
            return _soap_response(self._r_capabilities(), soap12)
        if action == "GetScopes":
            return _soap_response(self._r_scopes(), soap12)
        if action == "GetNetworkInterfaces":
            return _soap_response(self._r_network_interfaces(), soap12)
        if action == "GetUsers":
            return _soap_response(self._r_users(), soap12)
        if action == "GetServices":
            return _soap_response(self._r_services(), soap12)
        if action == "GetHostname":
            return _soap_response(self._r_hostname(), soap12)
        if action == "GetDNS":
            return _soap_response(self._r_dns(), soap12)
        if action == "GetNTP":
            return _soap_response(self._r_ntp(), soap12)
        if action == "GetNetworkProtocols":
            return _soap_response(self._r_network_protocols(), soap12)
        if action == "GetSystemDateAndTime" or action == "GetDeviceTime":
            return _soap_response(self._r_get_system_date_time(), soap12)
        if action == "SetSystemDateAndTime":
            return _soap_response(ET.Element(_q("SetSystemDateAndTimeResponse", NS["tds"])), soap12)
        if action == "GetDiscoveryMode":
            return _soap_response(self._r_discovery_mode(), soap12)
        if action == "SetDiscoveryMode":
            return _soap_response(ET.Element(_q("SetDiscoveryModeResponse", NS["tds"])), soap12)
        if action == "SystemReboot":
            return _soap_response(self._r_reboot(), soap12)
        if action == "GetSystemLog":
            return _soap_response(self._r_system_log(), soap12)
        if action == "GetWsdlUrl":
            return _soap_response(self._r_wsdl_url(), soap12)
        if action == "GetDeviceTime":
            return _soap_response(self._r_get_system_date_time(), soap12)
        if action == "GetServiceCapabilities":
            # 按命名空间返回对应服务的能力
            if ns_uri == NS["trt"]:
                return _soap_response(self._r_media_caps(), soap12)
            if ns_uri == NS["trptz"]:
                return _soap_response(self._r_ptz_caps(), soap12)
            if ns_uri == NS["tev"]:
                return _soap_response(self._r_event_caps(), soap12)
            if ns_uri == NS["timg"]:
                return _soap_response(self._r_imaging_caps(), soap12)
            return _soap_response(self._r_device_caps(), soap12)
        # Media 服务
        if action == "GetProfiles":
            return _soap_response(self._r_profiles(), soap12)
        if action == "GetVideoSources":
            return _soap_response(self._r_video_sources(), soap12)
        if action == "GetVideoSourceConfigurations":
            return _soap_response(self._r_video_source_configs(), soap12)
        if action == "GetVideoSourceConfiguration":
            return _soap_response(self._r_video_source_configs(), soap12)
        if action == "GetVideoEncoderConfigurations":
            return _soap_response(self._r_encoder_configs(), soap12)
        if action == "GetVideoEncoderConfiguration":
            return _soap_response(self._r_encoder_configs(), soap12)
        if action == "GetVideoEncoderConfigurationOptions":
            return _soap_response(self._r_encoder_options(), soap12)
        if action == "GetStreamUri":
            pt = None
            pte = req.find(f".//{_q('ProfileToken', NS['trt'])}")
            if pte is not None:
                pt = pte.text
            return _soap_response(self._r_stream_uri(pt), soap12)
        if action == "GetSnapshotUri":
            return _soap_response(self._r_snapshot_uri(), soap12)
        if action == "GetVideoSourceConfigurationOptions":
            return _soap_response(self._r_video_source_cfg_opts(), soap12)
        if action == "GetGuaranteedNumberOfVideoEncoderInstances":
            return _soap_response(self._r_encoder_instances(), soap12)
        if action == "GetAudioSources":
            return _soap_response(self._r_audio_sources(), soap12)
        if action == "GetAudioEncoderConfigurations":
            return _soap_response(self._r_audio_encoder_configs(), soap12)
        if action == "GetMetadataConfigurations":
            return _soap_response(self._r_metadata_configs(), soap12)
        if action == "GetCompatibleVideoEncoderConfigurations":
            return _soap_response(self._r_encoder_configs(), soap12)
        # PTZ 服务
        if action == "GetNodes":
            return _soap_response(self._r_ptz_nodes(), soap12)
        if action == "GetConfigurations":
            return _soap_response(self._r_ptz_configs(), soap12)
        if action == "GetConfiguration":
            return _soap_response(self._r_ptz_configs(), soap12)
        if action == "GetStatus":
            return _soap_response(self._r_ptz_status(), soap12)
        if action == "ContinuousMove":
            return _soap_response(ET.Element(_q("ContinuousMoveResponse", NS["trptz"])), soap12)
        if action == "Stop":
            return _soap_response(ET.Element(_q("StopResponse", NS["trptz"])), soap12)
        if action == "AbsoluteMove":
            return _soap_response(ET.Element(_q("AbsoluteMoveResponse", NS["trptz"])), soap12)
        if action == "RelativeMove":
            return _soap_response(ET.Element(_q("RelativeMoveResponse", NS["trptz"])), soap12)
        if action == "GetPresets":
            return _soap_response(self._r_ptz_presets(), soap12)
        if action == "SetPreset":
            return _soap_response(self._r_ptz_set_preset(), soap12)
        if action == "RemovePreset":
            return _soap_response(ET.Element(_q("RemovePresetResponse", NS["trptz"])), soap12)
        # Events 服务
        if action == "GetEventProperties":
            return _soap_response(self._r_event_properties(), soap12)
        if action == "CreatePullPointSubscription":
            return _soap_response(self._r_pullpoint_sub(), soap12)
        if action == "PullMessages":
            return _soap_response(self._r_pull_messages(), soap12)
        if action == "GetCurrentMessage":
            return _soap_response(self._r_pull_messages(), soap12)
        if action == "Subscribe":
            return _soap_response(self._r_subscribe(), soap12)
        if action == "Renew":
            return _soap_response(self._r_renew(), soap12)
        if action == "Unsubscribe":
            return _soap_response(ET.Element(_q("UnsubscribeResponse", NS["wsnt"])), soap12)
        # Imaging 服务
        if action == "GetImagingSettings":
            return _soap_response(self._r_imaging_settings(), soap12)
        if action == "GetOptions":
            return _soap_response(self._r_imaging_options(), soap12)
        if action == "SetImagingSettings":
            return _soap_response(ET.Element(_q("SetImagingSettingsResponse", NS["timg"])), soap12)
        if action == "Move":
            return _soap_response(ET.Element(_q("MoveResponse", NS["timg"])), soap12)
        if action == "Stop":
            return _soap_response(ET.Element(_q("StopResponse", NS["timg"])), soap12)
        # 未知
        self._log("soap", f"未支持的操作: {action}", ok=False)
        return _soap_fault(f"Operation not supported by simulator: {action}", soap12)

    # ── Device 响应构造 ──
    def _now_str(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    def _r_get_system_date_time(self) -> ET.Element:
        r = ET.Element(_q("GetSystemDateAndTimeResponse", NS["tds"]))
        sdt = ET.SubElement(r, _q("SystemDateAndTime", NS["tt"]))
        _child(sdt, "DateTimeType", NS["tt"], "Manual")
        _child(sdt, "DaylightSavings", NS["tt"], "false")
        tz = ET.SubElement(sdt, _q("TimeZone", NS["tt"]))
        _child(tz, "TZ", NS["tt"], "CST-8:00:00")
        now = time.localtime()
        utc = ET.SubElement(sdt, _q("UTCDateTime", NS["tt"]))
        d = ET.SubElement(utc, _q("Date", NS["tt"]))
        _child(d, "Year", NS["tt"], now.tm_year)
        _child(d, "Month", NS["tt"], now.tm_mon)
        _child(d, "Day", NS["tt"], now.tm_mday)
        t = ET.SubElement(utc, _q("Time", NS["tt"]))
        _child(t, "Hour", NS["tt"], now.tm_hour)
        _child(t, "Minute", NS["tt"], now.tm_min)
        _child(t, "Second", NS["tt"], now.tm_sec)
        return r

    def _r_device_information(self) -> ET.Element:
        r = ET.Element(_q("GetDeviceInformationResponse", NS["tds"]))
        _child(r, "Manufacturer", NS["tds"], self.manufacturer)
        _child(r, "Model", NS["tds"], self.model)
        _child(r, "FirmwareVersion", NS["tds"], "5.5.8 build 20260818")
        _child(r, "SerialNumber", NS["tds"], self.serial)
        _child(r, "HardwareId", NS["tds"], "SIM-HW-001")
        return r

    def _r_capabilities(self) -> ET.Element:
        r = ET.Element(_q("GetCapabilitiesResponse", NS["tds"]))
        caps = ET.SubElement(r, _q("Capabilities", NS["tt"]))
        # 子元素顺序严格按 onvif.xsd Capabilities 类型: Analytics, Device, Events, Imaging, Media, PTZ, Extension
        _child(caps, "Analytics", NS["tt"], "")
        dev = ET.SubElement(caps, _q("Device", NS["tt"]))
        _child(dev, "XAddr", NS["tt"], self.xaddr)
        _child(dev, "System", NS["tt"], "")
        _child(dev, "IO", NS["tt"], "")
        _child(dev, "Security", NS["tt"], "")
        _child(dev, "Network", NS["tt"], "")
        ev = ET.SubElement(caps, _q("Events", NS["tt"]))
        _child(ev, "XAddr", NS["tt"], self.event_xaddr)
        _child(ev, "WSSubscriptionManagerSupport", NS["tt"], "true")
        img = ET.SubElement(caps, _q("Imaging", NS["tt"]))
        _child(img, "XAddr", NS["tt"], self.imaging_xaddr)
        media = ET.SubElement(caps, _q("Media", NS["tt"]))
        _child(media, "XAddr", NS["tt"], self.media_xaddr)
        _child(media, "StreamingCapabilities", NS["tt"], "")
        ptz = ET.SubElement(caps, _q("PTZ", NS["tt"]))
        _child(ptz, "XAddr", NS["tt"], self.ptz_xaddr)
        return r

    def _r_scopes(self) -> ET.Element:
        r = ET.Element(_q("GetScopesResponse", NS["tds"]))
        scopes = [
            ("ScopeName", f"onvif://www.onvif.org/name/{self.device_name}"),
            ("Hardware", f"onvif://www.onvif.org/hardware/{self.model}"),
            ("Location", "onvif://www.onvif.org/location/China"),
            ("VideoSourceMode", f"onvif://www.onvif.org/videomode/1"),
        ]
        for cfg, item in scopes:
            s = ET.SubElement(r, _q("Scopes", NS["tds"]))
            _child(s, "ScopeDef", NS["tt"], "Fixed" if cfg != "Location" else "Configurable")
            _child(s, "ScopeItem", NS["tt"], item)
        return r

    def _r_network_interfaces(self) -> ET.Element:
        r = ET.Element(_q("GetNetworkInterfacesResponse", NS["tds"]))
        ni = ET.SubElement(r, _q("NetworkInterfaces", NS["tds"]))
        ni.set("token", "eth0")
        _child(ni, "Enabled", NS["tt"], "true")
        info = ET.SubElement(ni, _q("Info", NS["tt"]))
        _child(info, "Name", NS["tt"], "eth0")
        _child(info, "HwAddress", NS["tt"], self.mac)
        _child(info, "MTU", NS["tt"], 1500)
        link = ET.SubElement(ni, _q("Link", NS["tt"]))
        _child(link, "AdminSettings", NS["tt"], "AutoNegotiate")
        _child(link, "OperSettings", NS["tt"], "AutoNegotiate")
        _child(link, "InterfaceType", NS["tt"], 6)
        ipv4 = ET.SubElement(ni, _q("IPv4", NS["tt"]))
        _child(ipv4, "Enabled", NS["tt"], "true")
        cfg = ET.SubElement(ipv4, _q("Config", NS["tt"]))
        _child(cfg, "Manual", NS["tt"], "true")
        li = ET.SubElement(cfg, _q("LinkLocal", NS["tt"]))
        _child(li, "Enabled", NS["tt"], "false")
        _child(cfg, "FromDHCP", NS["tt"], "false")
        _child(cfg, "DHCP", NS["tt"], "false")
        manual = ET.SubElement(cfg, _q("Manual", NS["tt"]))
        a = ET.SubElement(manual, _q("Address", NS["tt"]))
        _child(a, "Type", NS["tt"], "IPv4")
        _child(a, "IPv4", NS["tt"], self.host_ip)
        _child(manual, "PrefixLength", NS["tt"], 24)
        return r

    def _r_users(self) -> ET.Element:
        r = ET.Element(_q("GetUsersResponse", NS["tds"]))
        u = ET.SubElement(r, _q("User", NS["tds"]))
        _child(u, "Username", NS["tt"], self.username)
        _child(u, "UserLevel", NS["tt"], "Administrator")
        return r

    def _r_services(self) -> ET.Element:
        r = ET.Element(_q("GetServicesResponse", NS["tds"]))
        services = [
            ("http://www.onvif.org/ver10/device/wsdl", self.xaddr, "1.0"),
            ("http://www.onvif.org/ver10/media/wsdl", self.media_xaddr, "1.0"),
            ("http://www.onvif.org/ver10/ptz/wsdl", self.ptz_xaddr, "1.0"),
            ("http://www.onvif.org/ver10/events/wsdl", self.event_xaddr, "1.0"),
            ("http://www.onvif.org/ver10/imaging/wsdl", self.imaging_xaddr, "1.0"),
        ]
        for ns, xaddr, ver in services:
            s = ET.SubElement(r, _q("Service", NS["tds"]))
            _child(s, "Namespace", NS["tt"], ns)
            _child(s, "XAddr", NS["tt"], xaddr)
            v = ET.SubElement(s, _q("Version", NS["tt"]))
            _child(v, "Major", NS["tt"], 1)
            _child(v, "Minor", NS["tt"], 0)
        return r

    def _r_hostname(self) -> ET.Element:
        r = ET.Element(_q("GetHostnameResponse", NS["tds"]))
        h = ET.SubElement(r, _q("Hostname", NS["tt"]))
        _child(h, "FromDHCP", NS["tt"], "false")
        _child(h, "Name", NS["tt"], "tingtao-sim-cam")
        return r

    def _r_dns(self) -> ET.Element:
        r = ET.Element(_q("GetDNSResponse", NS["tds"]))
        d = ET.SubElement(r, _q("DNS", NS["tt"]))
        _child(d, "FromDHCP", NS["tt"], "false")
        _child(d, "SearchDomain", NS["tt"], "local")
        for dns_ip in ("223.5.5.5", "114.114.114.114"):
            di = ET.SubElement(d, _q("DNSManual", NS["tt"]))
            _child(di, "Type", NS["tt"], "IPv4")
            _child(di, "IPv4Address", NS["tt"], dns_ip)
        return r

    def _r_ntp(self) -> ET.Element:
        r = ET.Element(_q("GetNTPResponse", NS["tds"]))
        n = ET.SubElement(r, _q("NTP", NS["tt"]))
        _child(n, "FromDHCP", NS["tt"], "false")
        ni = ET.SubElement(n, _q("NTPManual", NS["tt"]))
        _child(ni, "Type", NS["tt"], "DNS")
        _child(ni, "IPv4Address", NS["tt"], "ntp.aliyun.com")
        return r

    def _r_network_protocols(self) -> ET.Element:
        r = ET.Element(_q("GetNetworkProtocolsResponse", NS["tds"]))
        for name, port in (("HTTP", self.http_port), ("RTSP", self.rtsp_port), ("ONVIF", self.http_port)):
            p = ET.SubElement(r, _q("NetworkProtocols", NS["tds"]))
            _child(p, "Name", NS["tt"], name)
            _child(p, "Enabled", NS["tt"], "true")
            _child(p, "Port", NS["tt"], port)
        return r

    def _r_discovery_mode(self) -> ET.Element:
        r = ET.Element(_q("GetDiscoveryModeResponse", NS["tds"]))
        _child(r, "DiscoveryMode", NS["tds"], "Discoverable")
        return r

    def _r_reboot(self) -> ET.Element:
        r = ET.Element(_q("SystemRebootResponse", NS["tds"]))
        _child(r, "RebootTime", NS["tt"], self._now_str())
        return r

    def _r_system_log(self) -> ET.Element:
        r = ET.Element(_q("GetSystemLogResponse", NS["tds"]))
        sl = ET.SubElement(r, _q("SystemLog", NS["tt"]))
        _child(sl, "Binary", NS["tt"], base64.b64encode(b"Tingtao SIM camera system log").decode())
        _child(sl, "String", NS["tt"], "Tingtao SIM camera system log\n")
        return r

    def _r_wsdl_url(self) -> ET.Element:
        r = ET.Element(_q("GetWsdlUrlResponse", NS["tds"]))
        _child(r, "WsdlUrl", NS["tt"], f"{self.xaddr}?wsdl")
        return r

    def _r_device_caps(self) -> ET.Element:
        r = ET.Element(_q("GetServiceCapabilitiesResponse", NS["tds"]))
        c = ET.SubElement(r, _q("Capabilities", NS["tds"]))
        _child(c, "Network", NS["tt"], "true")
        _child(c, "Security", NS["tt"], "true")
        _child(c, "System", NS["tt"], "true")
        _child(c, "IO", NS["tt"], "false")
        _child(c, "Discovery", NS["tt"], "true")
        return r

    # ── Media 响应构造 (子元素顺序严格按 ONVIF schema, zeep 按序解析) ──
    def _profile_el(self, token: str = "Profile_1", name: str = "MainStream",
                    w: Optional[int] = None, h: Optional[int] = None,
                    fps: Optional[int] = None, enc_token: str = "VideoEncoderConfig_1",
                    src_token: str = "VideoSource_1", src_cfg: str = "VideoSourceConfig_1") -> ET.Element:
        w = w or self.video_width
        h = h or self.video_height
        fps = fps or self.fps
        p = ET.Element(_q("Profiles", NS["trt"]))
        p.set("token", token)
        _child(p, "Name", NS["tt"], name)
        # VideoSourceConfiguration
        vsc = ET.SubElement(p, _q("VideoSourceConfiguration", NS["tt"]))
        vsc.set("token", src_cfg)
        _child(vsc, "Name", NS["tt"], src_cfg)
        _child(vsc, "UseCount", NS["tt"], 1)
        src = ET.SubElement(vsc, _q("SourceToken", NS["tt"]))
        src.set("token", src_token)
        src.text = src_token
        bounds = ET.SubElement(vsc, _q("Bounds", NS["tt"]))
        bounds.set("x", "0")
        bounds.set("y", "0")
        bounds.set("width", str(w))
        bounds.set("height", str(h))
        # VideoEncoderConfiguration (顺序: Encoding, Resolution, Quality, RateControl, H264, Multicast, SessionTimeout)
        vec = ET.SubElement(p, _q("VideoEncoderConfiguration", NS["tt"]))
        vec.set("token", enc_token)
        _child(vec, "Name", NS["tt"], name)
        _child(vec, "UseCount", NS["tt"], 1)
        _child(vec, "Encoding", NS["tt"], self.codec)
        res = ET.SubElement(vec, _q("Resolution", NS["tt"]))
        _child(res, "Width", NS["tt"], w)
        _child(res, "Height", NS["tt"], h)
        _child(vec, "Quality", NS["tt"], 0.5)
        rc = ET.SubElement(vec, _q("RateControl", NS["tt"]))
        _child(rc, "FrameRateLimit", NS["tt"], fps)
        _child(rc, "EncodingInterval", NS["tt"], 1)
        _child(rc, "BitrateLimit", NS["tt"], self.bitrate_kbps)
        ET.SubElement(vec, _q("H264", NS["tt"]))
        ET.SubElement(vec, _q("Multicast", NS["tt"]))
        _child(vec, "SessionTimeout", NS["tt"], "PT60S")
        return p

    def _r_profiles(self) -> ET.Element:
        r = ET.Element(_q("GetProfilesResponse", NS["trt"]))
        # 主码流 Profile_1 / 子码流 Profile_2 (2026-09-01)
        r.append(self._profile_el("Profile_1", "MainStream",
                                  self.video_width, self.video_height, self.fps,
                                  "VideoEncoderConfig_1", "VideoSource_1", "VideoSourceConfig_1"))
        r.append(self._profile_el("Profile_2", "SubStream",
                                  self.sub_width, self.sub_height, self.sub_fps,
                                  "VideoEncoderConfig_2", "VideoSource_2", "VideoSourceConfig_2"))
        return r

    def _r_video_sources(self) -> ET.Element:
        r = ET.Element(_q("GetVideoSourcesResponse", NS["trt"]))
        for tok, w, h, fps in (("VideoSource_1", self.video_width, self.video_height, self.fps),
                               ("VideoSource_2", self.sub_width, self.sub_height, self.sub_fps)):
            vs = ET.SubElement(r, _q("VideoSources", NS["trt"]))
            vs.set("token", tok)
            _child(vs, "Framerate", NS["tt"], float(fps))
            res = ET.SubElement(vs, _q("Resolution", NS["tt"]))
            _child(res, "Width", NS["tt"], w)
            _child(res, "Height", NS["tt"], h)
            ET.SubElement(vs, _q("Imaging", NS["tt"]))
            ET.SubElement(vs, _q("Extension", NS["tt"]))
        return r

    def _r_video_source_configs(self) -> ET.Element:
        r = ET.Element(_q("GetVideoSourceConfigurationsResponse", NS["trt"]))
        vsc = ET.SubElement(r, _q("Configurations", NS["trt"]))
        vsc.set("token", "VideoSourceConfig_1")
        _child(vsc, "Name", NS["tt"], "VideoSourceConfig_1")
        _child(vsc, "UseCount", NS["tt"], 1)
        src = ET.SubElement(vsc, _q("SourceToken", NS["tt"]))
        src.set("token", "VideoSource_1")
        src.text = "VideoSource_1"
        bounds = ET.SubElement(vsc, _q("Bounds", NS["tt"]))
        bounds.set("x", "0")
        bounds.set("y", "0")
        bounds.set("width", str(self.video_width))
        bounds.set("height", str(self.video_height))
        return r

    def _r_encoder_configs(self) -> ET.Element:
        r = ET.Element(_q("GetVideoEncoderConfigurationsResponse", NS["trt"]))
        r.append(self._profile_el("Profile_1", "MainStream",
                                  self.video_width, self.video_height, self.fps,
                                  "VideoEncoderConfig_1", "VideoSource_1", "VideoSourceConfig_1")
                 .find(_q("VideoEncoderConfiguration", NS["tt"])))
        r.append(self._profile_el("Profile_2", "SubStream",
                                  self.sub_width, self.sub_height, self.sub_fps,
                                  "VideoEncoderConfig_2", "VideoSource_2", "VideoSourceConfig_2")
                 .find(_q("VideoEncoderConfiguration", NS["tt"])))
        return r

    def _r_encoder_options(self) -> ET.Element:
        r = ET.Element(_q("GetVideoEncoderConfigurationOptionsResponse", NS["trt"]))
        opts = ET.SubElement(r, _q("Options", NS["trt"]))
        _child(opts, "QualityRange", NS["tt"], "")
        if self.codec == "MJPEG":
            enc_opt = ET.SubElement(opts, _q("JPEG", NS["trt"]))
        else:
            enc_opt = ET.SubElement(opts, _q(self.codec, NS["trt"]))
        _child(enc_opt, "ResolutionAvailable", NS["tt"], f"{self.video_width}x{self.video_height}")
        _child(enc_opt, "ResolutionAvailable", NS["tt"], "1280x720")
        _child(enc_opt, "ResolutionAvailable", NS["tt"], "640x360")
        _child(enc_opt, "FrameRateRange", NS["tt"], "")
        _child(enc_opt, "EncodingIntervalRange", NS["tt"], "")
        _child(enc_opt, "GovLengthRange", NS["tt"], "")
        return r

    def _r_stream_uri(self, profile_token: Optional[str] = None) -> ET.Element:
        r = ET.Element(_q("GetStreamUriResponse", NS["trt"]))
        chan = "102" if (profile_token or "").lower() in ("profile_2", "substream") else "101"
        uri = ET.SubElement(r, _q("MediaUri", NS["tt"]))
        _child(uri, "Uri", NS["tt"],
               f"rtsp://{self.host_ip}:{self.rtsp_port}/Streaming/Channels/{chan}")
        _child(uri, "InvalidAfterConnect", NS["tt"], "false")
        _child(uri, "InvalidAfterReboot", NS["tt"], "false")
        _child(uri, "Timeout", NS["tt"], "PT10S")
        return r

    def _r_snapshot_uri(self) -> ET.Element:
        r = ET.Element(_q("GetSnapshotUriResponse", NS["trt"]))
        uri = ET.SubElement(r, _q("MediaUri", NS["tt"]))
        _child(uri, "Uri", NS["tt"], self.snapshot_url)
        _child(uri, "InvalidAfterConnect", NS["tt"], "false")
        _child(uri, "InvalidAfterReboot", NS["tt"], "false")
        _child(uri, "Timeout", NS["tt"], "PT10S")
        return r

    def _r_video_source_cfg_opts(self) -> ET.Element:
        r = ET.Element(_q("GetVideoSourceConfigurationOptionsResponse", NS["trt"]))
        opts = ET.SubElement(r, _q("Options", NS["trt"]))
        b = ET.SubElement(opts, _q("BoundsRange", NS["tt"]))
        _child(b, "XRange", NS["tt"], "")
        _child(b, "YRange", NS["tt"], "")
        _child(b, "WidthRange", NS["tt"], "")
        _child(b, "HeightRange", NS["tt"], "")
        return r

    def _r_encoder_instances(self) -> ET.Element:
        r = ET.Element(_q("GetGuaranteedNumberOfVideoEncoderInstancesResponse", NS["trt"]))
        _child(r, "TotalNumber", NS["tt"], 2)
        return r

    def _r_audio_sources(self) -> ET.Element:
        r = ET.Element(_q("GetAudioSourcesResponse", NS["trt"]))
        return r

    def _r_audio_encoder_configs(self) -> ET.Element:
        r = ET.Element(_q("GetAudioEncoderConfigurationsResponse", NS["trt"]))
        return r

    def _r_metadata_configs(self) -> ET.Element:
        r = ET.Element(_q("GetMetadataConfigurationsResponse", NS["trt"]))
        return r

    def _r_media_caps(self) -> ET.Element:
        r = ET.Element(_q("GetServiceCapabilitiesResponse", NS["trt"]))
        c = ET.SubElement(r, _q("Capabilities", NS["trt"]))
        _child(c, "SnapshotUri", NS["tt"], "true")
        _child(c, "Rotation", NS["tt"], "false")
        _child(c, "VideoSourceMode", NS["tt"], "false")
        _child(c, "OSD", NS["tt"], "false")
        _child(c, "TemporaryOSDText", NS["tt"], "false")
        _child(c, "ProfileCapabilities", NS["tt"], "")
        _child(c, "StreamingCapabilities", NS["tt"], "")
        return r

    # ── PTZ 响应构造 ──
    def _r_ptz_nodes(self) -> ET.Element:
        r = ET.Element(_q("GetNodesResponse", NS["trptz"]))
        n = ET.SubElement(r, _q("PTZNode", NS["trptz"]))
        n.set("token", "PTZNode_1")
        _child(n, "Name", NS["tt"], "PTZNode_1")
        _child(n, "FixedHomePosition", NS["tt"], "true")
        sp = ET.SubElement(n, _q("SupportedPTZSpaces", NS["tt"]))
        _child(sp, "AbsolutePanTiltPositionSpace", NS["tt"], "")
        _child(sp, "AbsoluteZoomPositionSpace", NS["tt"], "")
        _child(sp, "RelativePanTiltTranslationSpace", NS["tt"], "")
        _child(sp, "RelativeZoomTranslationSpace", NS["tt"], "")
        _child(sp, "ContinuousPanTiltVelocitySpace", NS["tt"], "")
        _child(sp, "ContinuousZoomVelocitySpace", NS["tt"], "")
        _child(sp, "PanTiltSpeedSpace", NS["tt"], "")
        _child(sp, "ZoomSpeedSpace", NS["tt"], "")
        _child(n, "MaximumNumberOfPresets", NS["tt"], 8)
        _child(n, "HomeSupported", NS["tt"], "true")
        return r

    def _r_ptz_configs(self) -> ET.Element:
        r = ET.Element(_q("GetConfigurationsResponse", NS["trptz"]))
        c = ET.SubElement(r, _q("PTZConfiguration", NS["trptz"]))
        c.set("token", "PTZConfig_1")
        _child(c, "Name", NS["tt"], "PTZConfig_1")
        _child(c, "UseCount", NS["tt"], 1)
        node = ET.SubElement(c, _q("NodeToken", NS["tt"]))
        node.set("token", "PTZNode_1")
        node.text = "PTZNode_1"
        _child(c, "DefaultContinuousPanTiltVelocitySpace", NS["tt"], "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace")
        _child(c, "DefaultContinuousZoomVelocitySpace", NS["tt"], "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace")
        _child(c, "DefaultPTZSpeed", NS["tt"], "")
        _child(c, "DefaultPTZTimeout", NS["tt"], "PT5S")
        _child(c, "PanTiltLimits", NS["tt"], "")
        _child(c, "ZoomLimits", NS["tt"], "")
        return r

    def _r_ptz_status(self) -> ET.Element:
        r = ET.Element(_q("GetStatusResponse", NS["trptz"]))
        st = ET.SubElement(r, _q("PTZStatus", NS["tt"]))
        pos = ET.SubElement(st, _q("Position", NS["tt"]))
        pt = ET.SubElement(pos, _q("PanTilt", NS["tt"]))
        pt.set("x", "0.0")
        pt.set("y", "0.0")
        z = ET.SubElement(pos, _q("Zoom", NS["tt"]))
        z.set("x", "1.0")
        mv = ET.SubElement(st, _q("MoveStatus", NS["tt"]))
        _child(mv, "PanTilt", NS["tt"], "IDLE")
        _child(mv, "Zoom", NS["tt"], "IDLE")
        _child(st, "UtcTime", NS["tt"], self._now_str())
        return r

    def _r_ptz_presets(self) -> ET.Element:
        r = ET.Element(_q("GetPresetsResponse", NS["trptz"]))
        for i, name in enumerate(("Home", "Gate")):
            p = ET.SubElement(r, _q("Preset", NS["trptz"]))
            p.set("token", f"Preset_{i + 1}")
            _child(p, "Name", NS["tt"], name)
            _child(p, "PTZPosition", NS["tt"], "")
        return r

    def _r_ptz_set_preset(self) -> ET.Element:
        r = ET.Element(_q("SetPresetResponse", NS["trptz"]))
        _child(r, "PresetToken", NS["tt"], f"Preset_{int(time.time()) % 100}")
        return r

    def _r_ptz_caps(self) -> ET.Element:
        r = ET.Element(_q("GetServiceCapabilitiesResponse", NS["trptz"]))
        c = ET.SubElement(r, _q("Capabilities", NS["trptz"]))
        _child(c, "EFlip", NS["tt"], "false")
        _child(c, "Reverse", NS["tt"], "false")
        _child(c, "GetCompatibleConfigurations", NS["tt"], "false")
        _child(c, "MoveStatus", NS["tt"], "true")
        _child(c, "DefaultPTZTimeout", NS["tt"], "true")
        return r

    # ── Events 响应构造 (顺序/命名空间按 events.wsdl: TopicNamespaceLocation, wsnt:FixedTopicSet, wstop:TopicSet, wsnt:TopicExpressionDialect, ...) ──
    def _r_event_properties(self) -> ET.Element:
        r = ET.Element(_q("GetEventPropertiesResponse", NS["tev"]))
        _child(r, "TopicNamespaceLocation", NS["tev"], "http://www.onvif.org/ver10/events/topics/topicns.xsd")
        _child(r, "FixedTopicSet", NS["wsnt"], "true")
        ts = ET.SubElement(r, _q("TopicSet", NS["wstop"]))
        _child(ts, "Message", NS["tev"], "")
        _child(r, "TopicExpressionDialect", NS["wsnt"], "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet")
        _child(r, "MessageContentFilterDialect", NS["wsnt"], "http://www.onvif.org/ver10/tev/messageContentFilter/MessageContent")
        _child(r, "ProducerPropertiesFilterDialect", NS["wsnt"], "http://www.onvif.org/ver10/tev/producerPropertiesFilter/ProducerPropertiesFilter")
        return r

    def _r_pullpoint_sub(self) -> ET.Element:
        r = ET.Element(_q("CreatePullPointSubscriptionResponse", NS["tev"]))
        ref = ET.SubElement(r, _q("SubscriptionReference", NS["wsnt"]))
        addr = ET.SubElement(ref, _q("Address", NS["wsa"]))
        addr.text = self.event_xaddr + f"?PullPoint={self.pullpoint_seq}"
        self.pullpoint_seq += 1
        return r

    def _r_pull_messages(self) -> ET.Element:
        r = ET.Element(_q("PullMessagesResponse", NS["tev"]))
        holder = ET.SubElement(r, _q("NotificationMessageHolderList", NS["tev"]))
        _child(holder, "CurrentTime", NS["wsnt"], self._now_str())
        _child(holder, "TerminationTime", NS["wsnt"], self._now_str())
        return r

    def _r_subscribe(self) -> ET.Element:
        r = ET.Element(_q("SubscribeResponse", NS["wsnt"]))
        ref = ET.SubElement(r, _q("SubscriptionReference", NS["wsnt"]))
        _child(ref, "Address", NS["wsa"], self.event_xaddr + f"?Sub={self.pullpoint_seq}")
        self.pullpoint_seq += 1
        return r

    def _r_renew(self) -> ET.Element:
        r = ET.Element(_q("RenewResponse", NS["wsnt"]))
        _child(r, "TerminationTime", NS["wsnt"], self._now_str())
        return r

    def _r_event_caps(self) -> ET.Element:
        r = ET.Element(_q("GetServiceCapabilitiesResponse", NS["tev"]))
        c = ET.SubElement(r, _q("Capabilities", NS["tev"]))
        _child(c, "WSSubscriptionPolicySupport", NS["tev"], "true")
        _child(c, "WSPullPointSupport", NS["tev"], "true")
        _child(c, "WSPausableSubscriptionManagerInterfaceSupport", NS["tev"], "false")
        return r

    # ── Imaging 响应构造 ──
    def _r_imaging_settings(self) -> ET.Element:
        r = ET.Element(_q("GetImagingSettingsResponse", NS["timg"]))
        s = ET.SubElement(r, _q("ImagingSettings", NS["tt"]))
        _child(s, "Brightness", NS["tt"], 50)
        _child(s, "ColorSaturation", NS["tt"], 50)
        _child(s, "Contrast", NS["tt"], 50)
        _child(s, "Sharpness", NS["tt"], 50)
        b = ET.SubElement(s, _q("BacklightCompensation", NS["tt"]))
        _child(b, "Mode", NS["tt"], "OFF")
        _child(s, "Exposure", NS["tt"], "")
        _child(s, "Focus", NS["tt"], "")
        _child(s, "IrCutFilter", NS["tt"], "AUTO")
        _child(s, "WideDynamicRange", NS["tt"], "")
        _child(s, "WhiteBalance", NS["tt"], "")
        return r

    def _r_imaging_options(self) -> ET.Element:
        r = ET.Element(_q("GetOptionsResponse", NS["timg"]))
        o = ET.SubElement(r, _q("ImagingOptions", NS["tt"]))
        _child(o, "BacklightCompensation", NS["tt"], "")
        _child(o, "Exposure", NS["tt"], "")
        _child(o, "Focus", NS["tt"], "")
        _child(o, "IrCutFilterModes", NS["tt"], "AUTO")
        _child(o, "WhiteBalance", NS["tt"], "")
        return r

    def _r_imaging_caps(self) -> ET.Element:
        r = ET.Element(_q("GetServiceCapabilitiesResponse", NS["timg"]))
        c = ET.SubElement(r, _q("Capabilities", NS["timg"]))
        _child(c, "ImageStabilization", NS["tt"], "false")
        _child(c, "IrCutFilterAutoAdjustment", NS["tt"], "false")
        return r
