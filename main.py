# -*- coding: utf-8 -*-
"""
服务器在线看板 —— AstrBot 插件

盯着我的世界服务器，隔一会儿问一次「现在谁在线」，把结果按天存下来，
然后在面板「扩展功能」里画成看板：谁什么时候来、什么时候走、
每个小时几个人、哪个时段最热闹、谁玩得最久。

取数三条路，挑得开就用哪条：
  1. 查询协议（GameSpy4 / UDP）—— 能拿到全部在线人名，服务器要开 enable-query
  2. RCON（TCP）       —— 也能拿全名单，要 server.properties 里开 rcon 并设密码
  3. 服务器列表（SLP）  —— 谁都不用配，但只能拿到人数和一部分名字
「自动」就是按 1 → 2 → 3 的顺序试。
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import socket
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from astrbot.api import logger
except Exception:  # 本地离线测试时没有 astrbot
    import logging

    logger = logging.getLogger("mc_online")

try:
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
except Exception:  # pragma: no cover
    AstrMessageEvent = Any  # type: ignore

    class _假filter:
        @staticmethod
        def command(*_a, **_k):
            def 包(fn):
                return fn

            return 包

        @staticmethod
        def on_llm_response(*_a, **_k):
            def 包(fn):
                return fn

            return 包

    filter = _假filter  # type: ignore

    class Context:  # type: ignore
        pass

    class Star:  # type: ignore
        def __init__(self, *a, **k):
            pass

    def register(*_a, **_k):
        def 包(cls):
            return cls

        return 包


def _消息过滤():
    """「所有消息」过滤器的装饰器。

    各版本里 EventMessageType 的取值名不完全一样，这里能认几个认几个；
    一个都认不出来就不给参数，让它按默认（全部消息）挂。
    """
    类 = getattr(filter, "EventMessageType", None)
    for 名 in ("ALL", "ALL_MESSAGES", "ALL_MESSAGE"):
        值 = getattr(类, 名, None) if 类 is not None else None
        if 值 is not None:
            return _deco("event_message_type", 值)
    return _deco("event_message_type")


PLUGIN_DIR = Path(__file__).resolve().parent
插件名 = "astrbot_plugin_mc_online"


# ---------------------------------------------------------------- 小工具


def _deco(名: str, *参数, **关键字):
    """AstrBot 版本之间 filter 上的东西不一样，取不到就当装饰器不存在。"""
    try:
        目标 = getattr(filter, 名)
        return 目标(*参数, **关键字)
    except Exception:

        def 空(fn):
            return fn

        return 空


_时区: Optional[Any] = None
_时区名 = "系统本地"


def _设时区(值: Any) -> str:
    """把配置里的时区字符串变成时区对象，看不懂就用系统本地。"""
    global _时区, _时区名
    原 = "" if 值 is None else str(值).strip()
    小 = 原.lower()
    if 小 in ("", "local", "system", "本地", "系统"):
        _时区 = None
        _时区名 = "系统本地"
        return _时区名
    if 小 in ("utc", "gmt", "z", "utc+0"):
        _时区 = timezone.utc
        _时区名 = "UTC"
        return _时区名
    if 小 in ("asia/shanghai", "beijing", "北京", "北京时间", "prc", "cst", "utc+8", "+8"):
        _时区 = timezone(timedelta(hours=8))
        _时区名 = "UTC+8"
        return _时区名
    匹配 = re.match(r"^(?:utc)?([+-])(\d{1,2})(?::?(\d{2}))?$", 小)
    if 匹配:
        符号 = 1 if 匹配.group(1) == "+" else -1
        时 = int(匹配.group(2))
        分 = int(匹配.group(3) or 0)
        if 0 <= 时 <= 14:
            _时区 = timezone(符号 * timedelta(hours=时, minutes=分))
            _时区名 = "UTC%s%d%s" % (
                匹配.group(1),
                时,
                (":%02d" % 分) if 分 else "",
            )
            return _时区名
    _时区 = None
    _时区名 = "系统本地"
    return _时区名


def _本地(时间戳: float) -> datetime:
    if _时区 is None:
        return datetime.fromtimestamp(时间戳)
    return datetime.fromtimestamp(时间戳, tz=_时区)


def _现在() -> float:
    return time.time()


def _日期串(时间戳: float) -> str:
    return _本地(时间戳).strftime("%Y-%m-%d")


def _今天零点() -> float:
    今 = _本地(_现在())
    零 = 今.replace(hour=0, minute=0, second=0, microsecond=0)
    return 零.timestamp()


def _日零点(日: str) -> float:
    """把 YYYY-MM-DD 变成那个时区当天零点的时间戳。"""
    try:
        天 = datetime.strptime(日, "%Y-%m-%d")
    except Exception:
        return _今天零点()
    if _时区 is not None:
        天 = 天.replace(tzinfo=_时区)
    return 天.timestamp()


def _本周一零点() -> float:
    今 = _本地(_现在())
    零 = 今.replace(hour=0, minute=0, second=0, microsecond=0)
    return (零 - timedelta(days=零.weekday())).timestamp()


def _说时间(时间戳: float, 带年: bool = False) -> str:
    格式 = "%Y-%m-%d %H:%M:%S" if 带年 else "%m-%d %H:%M:%S"
    try:
        return _本地(float(时间戳)).strftime(格式)
    except Exception:
        return "--"


def _说短(秒: float) -> str:
    """卡片上用的紧凑时长：2分、1时12分、3时。"""
    秒 = max(0, int(秒))
    if 秒 < 60:
        return "%d 秒" % 秒
    if 秒 < 3600:
        return "%d 分" % (秒 // 60)
    时 = 秒 // 3600
    分 = (秒 % 3600) // 60
    return ("%d 时 %d 分" % (时, 分)) if 分 else ("%d 时" % 时)


def _折行(画: Any, 文本: str, 字体: Any, 最大宽: int) -> List[str]:
    """按像素宽度折行，逗号处优先断。"""
    行们: List[str] = []
    当前 = ""
    for 块 in 文本.replace("、", "、\n").split("\n"):
        if not 块:
            continue
        试 = 当前 + 块
        try:
            宽 = 画.textlength(试, font=字体)
        except Exception:
            宽 = len(试) * 20
        if 宽 <= 最大宽 or not 当前:
            当前 = 试
        else:
            行们.append(当前)
            当前 = 块
    if 当前:
        行们.append(当前)
    return 行们 or [文本]


def _说时长(秒: float) -> str:
    秒 = max(0, int(秒))
    if 秒 < 60:
        return "%d 秒" % 秒
    分 = 秒 // 60
    if 分 < 60:
        return "%d 分钟" % 分
    时 = 分 // 60
    分 = 分 % 60
    if 时 < 24:
        return "%d 小时 %d 分" % (时, 分) if 分 else "%d 小时" % 时
    天 = 时 // 24
    return "%d 天 %d 小时" % (天, 时 % 24)


def _整数(值: Any, 默认: int) -> int:
    """配置里可能是数字、字符串数字，甚至被别处塞了奇怪的值，一律不炸。"""
    try:
        return int(float(str(值).strip()))
    except Exception:
        return 默认


def _开关(值: Any) -> bool:
    if isinstance(值, bool):
        return 值
    if 值 is None:
        return False
    return str(值).strip().lower() not in ("0", "false", "no", "off", "", "关闭", "否")


def _说数(数: Any) -> str:
    """一万以下老实写数字，一万以上用万，一亿以上用亿。"""
    try:
        数 = float(数)
    except Exception:
        return "0"
    号 = "-" if 数 < 0 else ""
    数 = abs(数)
    if 数 < 10000:
        整数 = int(round(数))
        return 号 + str(整数)
    if 数 < 100000000:
        万 = 数 / 10000.0
        if 万 >= 100:
            return "%s%.0f万" % (号, 万)
        return ("%s%.2f万" % (号, 万)).replace(".00", "").replace("0万", "万")
    亿 = 数 / 100000000.0
    return ("%s%.2f亿" % (号, 亿)).replace(".00", "").replace("0亿", "亿")


# ---------------------------------------------------------------- 取在线（三种协议）


def _包长度_varint(数: int) -> bytes:
    出 = bytearray()
    数 = int(数)
    while True:
        小 = 数 & 0x7F
        数 >>= 7
        if 数:
            出.append(小 | 0x80)
        else:
            出.append(小)
            return bytes(出)


def _读varint(数据: bytes, 位置: int) -> Tuple[int, int]:
    值 = 0
    位移 = 0
    while True:
        若 = 数据[位置]
        位置 += 1
        值 |= (若 & 0x7F) << 位移
        if not 若 & 0x80:
            return 值, 位置
        位移 += 7
        if 位移 > 35:
            raise ValueError("varint 太长")


async def _读varint流(读: Any) -> Tuple[int, int]:
    值 = 0
    位移 = 0
    while True:
        若 = await 读.readexactly(1)
        值 |= (若[0] & 0x7F) << 位移
        if not 若[0] & 0x80:
            return 值, 位移
        位移 += 7
        if 位移 > 35:
            raise ValueError("varint 太长")


async def _问列表(主机: str, 端口: int, 超时: float = 5.0) -> Optional[Dict[str, Any]]:
    """服务器列表协议（Server List Ping）—— 谁都不用配，一定能试。"""
    try:
        读, 写 = await asyncio.wait_for(asyncio.open_connection(主机, 端口), 超时)
        try:
            主机串 = 主机.encode("utf-8")
            握手 = (
                _包长度_varint(0)
                + _包长度_varint(765)
                + _包长度_varint(len(主机串))
                + 主机串
                + struct.pack(">H", int(端口))
                + _包长度_varint(1)
            )
            写.write(_包长度_varint(len(握手)) + 握手 + b"\x01\x00")
            await 写.drain()

            长, _ = await asyncio.wait_for(_读varint流(读), 超时)
            if 长 <= 0 or 长 > (1 << 20):
                raise ValueError("长度不对")
            体 = await asyncio.wait_for(读.readexactly(长), 超时)
            if not 体 or 体[0] != 0:
                raise ValueError("不是状态包")
            串长, 位置 = _读varint(体, 1)
            文本 = 体[位置 : 位置 + 串长].decode("utf-8", "replace")
            信息 = json.loads(文本)
            玩家 = 信息.get("players") or {}
            人数 = int(玩家.get("online") or 0)
            上限 = int(玩家.get("max") or 0)
            名单: List[str] = []
            for 条 in 玩家.get("sample") or []:
                if isinstance(条, dict):
                    名 = 条.get("name")
                    if 名:
                        名单.append(str(名))
            return {"人数": 人数, "上限": 上限, "名单": 名单, "来源": "服务器列表"}
        finally:
            try:
                写.close()
            except Exception:
                pass
    except Exception:
        pass
    # 老版本服务器：发一个 0xFE，回来一串 UTF-16BE
    try:
        return await asyncio.to_thread(_问旧版列表, 主机, 端口, 超时)
    except Exception:
        return None


def _问旧版列表(主机: str, 端口: int, 超时: float) -> Optional[Dict[str, Any]]:
    套 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    套.settimeout(超时)
    try:
        套.connect((主机, int(端口)))
        套.sendall(b"\xFE\x01")
        块 = 套.recv(4096)
        if not 块 or 块[0] != 0xFF:
            return None
        文本 = 块[3:].decode("utf-16-be", "replace")
        段 = 文本.split("\x00")
        if len(段) < 6:
            return None
        return {
            "人数": int(段[-2] or 0),
            "上限": int(段[-1] or 0),
            "名单": [],
            "来源": "服务器列表（旧版）",
        }
    finally:
        套.close()


def _问查询协议(主机: str, 端口: int, 超时: float = 4.0) -> Optional[Dict[str, Any]]:
    """GameSpy4 查询协议，能拿到完整在线名单。"""
    套 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    套.settimeout(超时)
    令牌 = b"\x01\x02\x03\x04"
    try:
        套.sendto(b"\xFE\xFD\x09" + 令牌, (主机, int(端口)))
        数据, _ = 套.recvfrom(4096)
        if not 数据 or 数据[0] != 0x09 or len(数据) < 9:
            return None
        挑战 = 数据[5:9]
        套.sendto(b"\xFE\xFD\x00" + 令牌 + 挑战 + b"\x00\x00\x00\x00", (主机, int(端口)))
        数据, _ = 套.recvfrom(65535)
        if not 数据 or 数据[0] != 0x00:
            return None
        正文 = 数据[16:] if len(数据) > 16 else b""
        段 = 正文.split(b"\x00")
        键值: Dict[str, str] = {}
        i = 0
        while i + 1 < len(段):
            键 = 段[i].decode("utf-8", "replace")
            值 = 段[i + 1].decode("utf-8", "replace")
            键值[键] = 值
            i += 2
        名单: List[str] = []
        如果在 = -1
        for 位置2, 块 in enumerate(段):
            if 块.endswith(b"player_"):
                如果在 = 位置2
                break
        if 如果在 >= 0:
            for 块 in 段[如果在 + 1 :]:
                名 = 块.decode("utf-8", "replace").strip()
                if 名:
                    名单.append(名)
        return {
            "人数": int(键值.get("numplayers") or len(名单)),
            "上限": int(键值.get("maxplayers") or 0),
            "名单": 名单,
            "来源": "查询协议",
        }
    except Exception:
        return None
    finally:
        套.close()


def _rcon包(请求号: int, 类型: int, 正文: str) -> bytes:
    体 = 正文.encode("utf-8")
    长度 = 4 + 4 + len(体) + 2
    return struct.pack("<iii", 长度, 请求号, 类型) + 体 + b"\x00\x00"


async def _rcon收(读: Any, 超时: float) -> Optional[Tuple[int, str]]:
    try:
        头 = await asyncio.wait_for(读.readexactly(4), 超时)
    except Exception:
        return None
    长 = struct.unpack("<i", 头)[0]
    if 长 < 8 or 长 > (1 << 20):
        return None
    体 = await asyncio.wait_for(读.readexactly(长), 超时)
    请求号, 类型 = struct.unpack("<ii", 体[:8])
    文本 = 体[8:-2].decode("utf-8", "replace") if len(体) > 10 else ""
    return 请求号, 文本


def _解名单(文本: str) -> Dict[str, Any]:
    名单: List[str] = []
    人数 = 0
    上限 = 0
    匹配 = re.search(r"(\d+)\s*of\s*a\s*max\s*of\s*(\d+)", 文本, re.I)
    if 匹配:
        人数 = int(匹配.group(1))
        上限 = int(匹配.group(2))
    否则 = re.search(r"有\s*(\d+)\s*个.*?上限\s*(\d+)", 文本)
    if not 匹配 and 否则:
        人数 = int(否则.group(1))
        上限 = int(否则.group(2))
    if ":" in 文本:
        尾 = 文本.split(":", 1)[1]
        名单 = [x.strip() for x in 尾.split(",") if x.strip()]
    if not 人数:
        人数 = len(名单)
    return {"人数": 人数, "上限": 上限, "名单": 名单, "来源": "RCON"}


async def _问RCON(主机: str, 端口: int, 密码: str, 超时: float = 5.0) -> Optional[Dict[str, Any]]:
    try:
        读, 写 = await asyncio.wait_for(asyncio.open_connection(主机, int(端口)), 超时)
    except Exception:
        return None
    try:
        写.write(_rcon包(1, 3, 密码))
        await 写.drain()
        认证 = await _rcon收(读, 超时)
        if 认证 is None or 认证[0] == -1:
            return None
        写.write(_rcon包(2, 2, "list"))
        await 写.drain()
        回 = await _rcon收(读, 超时)
        if 回 is None:
            return None
        return _解名单(回[1])
    except Exception:
        return None
    finally:
        try:
            写.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 账本


class 在线账本:
    """按天存两种东西：采样（那一会儿几个人）和流水（谁进谁出）。"""

    def __init__(self, 根: Path, 留存天数: int = 90):
        self.根 = Path(根)
        self.留存天数 = max(1, int(留存天数 or 90))
        self._目录们 = {}  # 服名 -> (采样目录, 流水目录)
        self._开目录("默认")

    def _开目录(self, 服名: str):
        """每台服一套子目录；旧的单服数据在根目录，也认。"""
        if 服名 in self._目录们:
            return self._目录们[服名]
        if 服名 == "默认":
            # 兼容旧数据：单服时代的文件直接躺在 采样/ 流水/ 下
            采样 = self.根 / "采样"
            流水 = self.根 / "流水"
        else:
            采样 = self.根 / "采样" / 服名
            流水 = self.根 / "流水" / 服名
        for 目录 in (采样, 流水):
            目录.mkdir(parents=True, exist_ok=True)
        self._目录们[服名] = (采样, 流水)
        return self._目录们[服名]

    def _目录(self, 服名: str, 名字: str) -> Path:
        采样, 流水 = self._开目录(服名)
        return 采样 if 名字 == "采样" else 流水

    def 写一条(self, 目录: Path, 条目: Dict[str, Any]) -> None:
        日 = _日期串(float(条目.get("t") or _现在()))
        文件 = 目录 / (日 + ".jsonl")
        with open(文件, "a", encoding="utf-8") as 柄:
            柄.write(json.dumps(条目, ensure_ascii=False) + "\n")

    def 记采样(self, 条目: Dict[str, Any], 服名: str = "默认") -> None:
        self.写一条(self._目录(服名, "采样"), 条目)

    def 记流水(self, 条目: Dict[str, Any], 服名: str = "默认") -> None:
        self.写一条(self._目录(服名, "流水"), 条目)

    def 读一天(self, 目录: Path, 日: str) -> List[Dict[str, Any]]:
        文件 = 目录 / (日 + ".jsonl")
        出: List[Dict[str, Any]] = []
        if not 文件.exists():
            return 出
        try:
            with open(文件, "r", encoding="utf-8") as 柄:
                for 行 in 柄:
                    行 = 行.strip()
                    if not 行:
                        continue
                    try:
                        条 = json.loads(行)
                    except Exception:
                        continue
                    if isinstance(条, dict):
                        出.append(条)
        except Exception as 错:
            logger.warning("[在线看板] 读 %s 失败：%s", 文件.name, 错)
        return 出

    def 读区间(self, 名字: str, 起: float, 止: float, 服名: str = "默认") -> List[Dict[str, Any]]:
        目录 = self._目录(服名, 名字)
        出: List[Dict[str, Any]] = []
        天 = _日零点(_日期串(起))
        while 天 <= 止 + 86400:
            日 = _日期串(天)
            for 条 in self.读一天(目录, 日):
                try:
                    时 = float(条.get("t") or 0)
                except Exception:
                    continue
                if 起 <= 时 <= 止:
                    出.append(条)
            天 += 86400
        出.sort(key=lambda x: float(x.get("t") or 0))
        return 出

    def 清过期(self) -> int:
        删 = 0
        界 = _现在() - self.留存天数 * 86400
        self._开目录("默认")
        全部目录 = []
        for _, (采样, 流水) in list(self._目录们.items()):
            全部目录 += [采样, 流水]
        # 兜底：多服子目录哪怕没被开过也扫一遍
        for 顶层 in ("采样", "流水"):
            T = self.根 / 顶层
            if T.is_dir():
                全部目录.append(T)
                for 子 in T.iterdir():
                    if 子.is_dir():
                        全部目录.append(子)
        for 目录 in 全部目录:
            for 文件 in 目录.glob("*.jsonl"):
                日 = 文件.stem
                try:
                    零 = _日零点(日)
                except Exception:
                    continue
                if 零 + 86400 <= 界:
                    try:
                        文件.unlink()
                        删 += 1
                    except Exception:
                        pass
        return 删


# ---------------------------------------------------------------- 插件本体


@register(插件名, "shierqiuxe", "记录我的世界服务器在线情况，画成看板", "v1.0.0")
class 在线看板插件(Star):
    def __init__(self, context: Context, config: Any = None, **kwargs):
        super().__init__(context)
        self.context = context
        self._配置对象 = config
        self.时区名 = _设时区(self._配置("time_zone", "UTC+8"))
        self.账 = 在线账本(_数据目录(), _整数(self._配置("keep_days", 90), 90))
        self.间隔 = max(20, _整数(self._配置("interval", 60), 60))
        self.服们: List[Dict[str, Any]] = _读服务器们(self._配置)
        self.要播报 = _开关(self._配置("announce", False))

        self._停 = False
        self._任务: Optional[asyncio.Task] = None
        self._锁 = threading.Lock()
        # 每台服一份独立状态，键是服名
        self._状态: Dict[str, Dict[str, Any]] = {}
        for 服 in self.服们:
            self._状态[服["name"]] = self._新状态()
        self._群们: set = set()
        self._指令时刻: float = 0.0  # 上一次处理 /在线 的时刻，用来防兜底通道重复回复
        self._开工时刻: float = _现在()  # 插件自己从什么时候开始跑的

        名单 = "、".join(服["name"] for 服 in self.服们) or "（没填）"
        logger.info(
            "[在线看板] 时区：%s，盯 %d 台服：%s，间隔：%d 秒",
            self.时区名, len(self.服们), 名单, self.间隔,
        )

    @staticmethod
    def _新状态() -> Dict[str, Any]:
        """一台服的运行时状态。"""
        return {
            "上次名单": set(),        # 上一轮在线的人
            "在线开始": {},            # 名字 -> 上线时刻
            "上次人数": None,
            "上次采样时刻": 0.0,
            "当前": {},                # 最近一次采样 {t,c,m,p,s}
            "来源": "",
            "错误": "",
            "已警告": False,
        }

    # ---------- 读配置

    def _配置(self, 键: str, 默认: Any = None) -> Any:
        """只认本插件自己的配置。

        以前这里会把整份 AstrBot 配置从头到尾翻一遍找键名，结果别的插件里
        也叫 interval 的配置（分段回复的 "1.5,3.5"）先被翻到，直接 int() 崩掉。
        现在只在「本插件的配置」这一层找，找不到就用默认值。
        """
        源们 = [self._配置对象, getattr(self, "config", None)]
        try:
            源们.append(self.context.get_config())
        except Exception:
            pass
        for 源 in 源们:
            if 源 is None:
                continue
            try:
                if isinstance(源, dict):
                    if 键 in 源:
                        return 源[键]
                    子 = 源.get(插件名)
                    if isinstance(子, dict) and 键 in 子:
                        return 子[键]
                else:
                    值 = getattr(源, 键, None)
                    if 值 is not None:
                        return 值
            except Exception:
                continue
        return 默认

    # ---------- 生命周期

    async def initialize(self) -> None:
        self._挂网页()
        try:
            self._任务 = asyncio.create_task(self._循环())
        except Exception as 错:
            logger.warning("[在线看板] 起后台循环失败：%s", 错)

    def _挂一个(self, 路: str, 处理: Any, 说明: str, 方法: Optional[List[str]] = None) -> bool:
        """AstrBot 不同版本 register_web_api 的参数个数不一样，挨个试。"""
        方法们 = list(方法 or ["GET"])
        for 试法 in (
            lambda: self.context.register_web_api(路, 处理, 方法们, 说明),
            lambda: self.context.register_web_api(路, 处理, 方法们),
            lambda: self.context.register_web_api(路, 处理),
        ):
            try:
                试法()
                return True
            except TypeError:
                continue
            except Exception as 错:
                logger.warning("[在线看板] 接口 %s 挂不上：%s", 路, 错)
                return False
        return False

    def _挂网页(self) -> None:
        # 每项都是四元组：(路由, 处理函数, 说明, 方法)。方法写 None 就是 GET。
        表 = [
            ("/%s/view" % 插件名, self.页面接口, "在线看板页面", None),
            ("/%s/summary" % 插件名, self.数据接口, "在线看板数据", None),
            ("/%s/health" % 插件名, self.自检接口, "自检", None),
            ("/%s/refresh" % 插件名, self.刷新接口, "立刻采样一次", ["POST"]),
            # 带服名的版本。桥 SDK 的端点不让带问号，多服时走路径传。
            ("/%s/summary/<sname>" % 插件名, self.数据接口, "看某台服务器的数据", None),
            ("/%s/refresh/<sname>" % 插件名, self.刷新接口, "立刻采样某台", ["POST"]),
        ]
        成功 = 0
        for 项 in 表:
            路, 处理, 说明 = 项[0], 项[1], 项[2]
            方法 = 项[3] if len(项) > 3 else None
            if self._挂一个(路, 处理, 说明, 方法):
                成功 += 1
        logger.info("[在线看板] 网页接口挂上 %d 个", 成功)

    async def terminate(self) -> None:
        self._停 = True
        if self._任务:
            self._任务.cancel()
            self._任务 = None

    # ---------- 采样循环

    async def _循环(self) -> None:
        await asyncio.sleep(6)  # 等 AstrBot 自己先起来
        while not self._停:
            for 服 in self.服们:
                if self._停:
                    return
                try:
                    await self._采一次(服)
                except asyncio.CancelledError:
                    return
                except Exception as 错:
                    态 = self._状态.setdefault(服["name"], self._新状态())
                    态["错误"] = str(错)[:200]
                    logger.warning("[在线看板] 采样 %s 出错：%s", 服["name"], 错)
            for _ in range(self.间隔):
                if self._停:
                    return
                await asyncio.sleep(1)

    async def _采一次(self, 服: Optional[Dict[str, Any]] = None) -> None:
        """采一台服。不传服就采第一台（兼容旧调用）。"""
        if 服 is None:
            服 = self.服们[0] if self.服们 else None
        if not 服:
            return
        名 = 服["name"]
        态 = self._状态.setdefault(名, self._新状态())
        结果 = await self._取在线(服)
        if 结果 is None:
            if not 态["已警告"]:
                logger.info("[在线看板] %s 没采到数据（地址 %s）", 名, 服["address"])
                态["已警告"] = True
            现在 = _现在()
            态["错误"] = "没连上（%s:%s）" % (服["address"], 服["game_port"])
            态["来源"] = ""
            # 连不上也记一笔，带 e=1。这样视图知道「这段时间是问不到」，
            # 曲线也不会把「没采到」当成「0 人」画平。
            if 现在 - 态["上次采样时刻"] >= 300 or 态["上次采样时刻"] <= 0:
                态["上次采样时刻"] = 现在
                try:
                    self.账.记采样({"t": 现在, "c": 0, "m": 0, "e": 1}, 名)
                except Exception:
                    pass
            return
        态["已警告"] = False
        现在 = _现在()
        人数 = int(结果.get("人数") or 0)
        上限 = int(结果.get("上限") or 0)
        名单 = [str(x) for x in (结果.get("名单") or [])]
        来源 = str(结果.get("来源") or "")
        态["来源"] = 来源
        态["错误"] = ""
        态["当前"] = {"t": 现在, "c": 人数, "m": 上限, "p": 名单, "s": 来源}

        with self._锁:
            新名单 = set(名单)
            进 = 新名单 - 态["上次名单"]
            出 = 态["上次名单"] - 新名单
            for 谁 in 进:
                态["在线开始"][谁] = 现在
                self.账.记流水({"t": 现在, "n": 谁, "a": "进"}, 名)
            for 谁 in 出:
                起 = 态["在线开始"].pop(谁, 现在)
                self.账.记流水({"t": 现在, "n": 谁, "a": "出", "d": round(现在 - 起, 1)}, 名)
            态["上次名单"] = 新名单

            # 采样只在人数变了、或者五分钟没存的时候落一条，省文件
            该存 = (
                态["上次人数"] is None
                or 人数 != 态["上次人数"]
                or 现在 - 态["上次采样时刻"] >= 300
            )
            if 该存:
                self.账.记采样({"t": 现在, "c": 人数, "m": 上限}, 名)
                态["上次人数"] = 人数
                态["上次采样时刻"] = 现在

        if self.要播报 and (进 or 出):
            for 谁 in 进:
                await self._播报("[%s] %s 上线了，现在 %d 人在线" % (名, 谁, 人数))
            for 谁 in 出:
                await self._播报("[%s] %s 下线了，现在 %d 人在线" % (名, 谁, 人数))

        if 现在 - getattr(self, "_上次清", 0) > 3600:
            self._上次清 = 现在
            try:
                self.账.清过期()
            except Exception:
                pass

    async def _取在线(self, 服: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if 服 is None:
            服 = self.服们[0] if self.服们 else None
        if not 服:
            return None
        地址 = 服["address"]
        模式 = 服.get("mode") or "自动"
        查询端口 = 服.get("query_port") or 0 or 服["game_port"]
        if 模式 in ("自动", "查询协议"):
            答 = await asyncio.to_thread(_问查询协议, 地址, 查询端口, 4.0)
            if 答:
                return 答
            if 模式 == "查询协议":
                return None
        if 模式 in ("自动", "RCON"):
            if 服.get("rcon_password"):
                答 = await _问RCON(地址, 服.get("rcon_port") or 25575,
                                   服["rcon_password"], 5.0)
                if 答:
                    return 答
            if 模式 == "RCON":
                return None
        答 = await _问列表(地址, 服["game_port"], 5.0)
        return 答

    async def _播报(self, 文本: str) -> None:
        if not self._群们:
            return
        try:
            from astrbot.api.message_components import Plain
            from astrbot.core.message.message_event_result import MessageChain
        except Exception:
            return
        for 会话 in list(self._群们):
            try:
                链 = MessageChain(chain=[Plain(文本)])
                await self.context.send_message(会话, 链)
            except Exception:
                continue

    # ---------- 汇总

    def _算汇总(self, 服名: str = "") -> Dict[str, Any]:
        """算一台服的汇总；服名空 = 第一台。"""
        if not 服名 or 服名 not in self._状态:
            服名 = self.服们[0]["name"] if self.服们 else "默认"
        服 = next((x for x in self.服们 if x["name"] == 服名), None)
        态 = self._状态.get(服名, self._新状态())
        现在 = _现在()
        今天零 = _今天零点()
        本周一 = _本周一零点()
        今天采样 = self.账.读区间("采样", 今天零, 现在, 服名)
        今天流水 = self.账.读区间("流水", 今天零, 现在, 服名)
        本周流水 = self.账.读区间("流水", 本周一, 现在, 服名)

        # 每小时平均在线（今天）
        每小时: List[Dict[str, Any]] = []
        分桶: Dict[int, List[int]] = {h: [] for h in range(24)}
        for 条 in 今天采样:
            if 条.get("e"):  # 连不上的那笔，不算进人数
                continue
            try:
                时 = _本地(float(条.get("t"))).hour
            except Exception:
                continue
            分桶[时].append(int(条.get("c") or 0))
        峰值 = 0
        峰值时 = 0
        for 时 in range(24):
            值 = 分桶[时]
            均 = sum(值) / len(值) if 值 else 0
            if 值 and max(值) > 峰值:
                峰值 = max(值)
                峰值时 = 时
            每小时.append({"时": 时, "均": round(均, 2), "峰": max(值) if 值 else 0, "样本": len(值)})

        # 近 24 小时（每小时取最大人数），给「曲线」用
        近24 = []
        本小时 = int(现在 // 3600) * 3600
        for 倒数 in range(23, -1, -1):
            起 = 本小时 - 倒数 * 3600
            止 = 起 + 3600
            值们 = []
            for 条 in self.账.读区间("采样", 起, 止 - 0.001, 服名):
                if 条.get("e"):
                    continue
                try:
                    值们.append(int(条.get("c") or 0))
                except Exception:
                    continue
            近24.append({
                "起": 起,
                "点": _本地(起).strftime("%H"),
                "峰": max(值们) if 值们 else 0,
                "有": bool(值们),
            })

        # 星期 × 小时 热力图（近 30 天）
        热力 = [[0.0 for _ in range(24)] for _ in range(7)]
        计数 = [[0 for _ in range(24)] for _ in range(7)]
        for 天偏移 in range(30):
            起 = 今天零 - 天偏移 * 86400
            止 = 起 + 86400
            for 条 in self.账.读区间("采样", 起, 止 - 0.001, 服名):
                if 条.get("e"):
                    continue
                try:
                    点 = _本地(float(条.get("t")))
                except Exception:
                    continue
                热力[点.weekday()][点.hour] += int(条.get("c") or 0)
                计数[点.weekday()][点.hour] += 1
        for 周 in range(7):
            for 时 in range(24):
                热力[周][时] = round(热力[周][时] / 计数[周][时], 2) if 计数[周][时] else 0.0

        # 今天来过的人
        今天来过 = set()
        for 条 in 今天流水:
            if 条.get("a") == "进":
                今天来过.add(str(条.get("n") or ""))
        for 名 in 态["上次名单"]:
            今天来过.add(名)
        今天来过.discard("")

        # 时长：今天 / 本周
        def 算时长(流水: List[Dict[str, Any]], 起: float, 止: float) -> Dict[str, float]:
            表: Dict[str, float] = {}
            for 条 in 流水:
                if 条.get("a") != "出":
                    continue
                名 = str(条.get("n") or "")
                if not 名:
                    continue
                try:
                    秒 = float(条.get("d") or 0)
                    结束时 = float(条.get("t") or 0)
                except Exception:
                    continue
                开始 = 结束时 - 秒
                左 = max(开始, 起)
                右 = min(结束时, 止)
                if 右 > 左:
                    表[名] = 表.get(名, 0.0) + (右 - 左)
            # 还在线的人，从上线算到现在
            for 名, 开始 in 态["在线开始"].items():
                左 = max(开始, 起)
                if 止 > 左:
                    表[名] = 表.get(名, 0.0) + (止 - 左)
            return 表

        今天时长 = 算时长(今天流水, 今天零, 现在)
        本周时长 = 算时长(本周流水, 本周一, 现在)

        def 排行(表: Dict[str, float], 几个: int = 10) -> List[Dict[str, Any]]:
            项 = sorted(表.items(), key=lambda x: -x[1])[:几个]
            return [
                {"名": 名, "秒": round(秒, 1), "说": _说时长(秒)}
                for 名, 秒 in 项
                if 秒 > 0
            ]

        最近流水 = []
        for 条 in list(reversed(今天流水))[:40]:
            最近流水.append(
                {
                    "时间": _说时间(float(条.get("t") or 0)),
                    "谁": str(条.get("n") or ""),
                    "动作": "进" if 条.get("a") == "进" else "出",
                    "时长": _说时长(float(条.get("d") or 0)) if 条.get("a") == "出" else "",
                }
            )

        现在名单 = sorted(态["上次名单"])
        在线的人 = []
        for 名 in 现在名单:
            起 = 态["在线开始"].get(名)
            在线的人.append({
                "名": 名,
                "在": _说时长(现在 - 起) if 起 else "",
                "秒": round(现在 - 起, 1) if 起 else 0,
            })
        return {
            "生成于": _说时间(现在),
            "运行秒": round(现在 - getattr(self, "_开工时刻", 现在), 1),
            "运行": _说时长(现在 - getattr(self, "_开工时刻", 现在)),
            "时区": self.时区名,
            "服务器": 服名,
            "服务器们": [x["name"] for x in self.服们],
            "地址": (服 or {}).get("address", ""),
            "模式": (服 or {}).get("mode", "自动"),
            "来源": 态["来源"],
            "错误": 态["错误"],
            "现在": {
                "人数": int(态["当前"].get("c") or 0),
                "上限": int(态["当前"].get("m") or 0),
                "名单": 现在名单,
                "人": 在线的人,
                "时间": _说时间(float(态["当前"].get("t") or 0)),
                "有数据": bool(态["当前"]),
                "采样于": float(态["当前"].get("t") or 0),
            },
            "今天": {
                "峰值": 峰值,
                "峰值时": 峰值时,
                "来过": len(今天来过),
                "进出": len(今天流水),
                "人时": round(sum(今天时长.values()) / 3600.0, 2),
                "时长说": _说时长(sum(今天时长.values())),
            },
            "每小时": 每小时,
            "近24": 近24,
            "热力": 热力,
            "排行今天": 排行(今天时长),
            "排行本周": 排行(本周时长, 15),
            "流水": 最近流水,
            "采样数": len(今天采样),
        }

    # ---------- 网页接口

    def _挑服名(self, 参数们, 关键字) -> str:
        """从各种传参姿势里挑出服务器名。

        AstrBot 各家版本的 _call_view 传法不一样：有的把路径参数按关键字塞
        （sname=...），有的按位置塞，有的给个 request。这里都摸一遍，认不出来
        就返回空（空 = 第一台）。
        """
        名们 = [服["name"] for 服 in self.服们]

        def 认(值) -> str:
            if 值 is None:
                return ""
            try:
                文 = urllib.parse.unquote(str(值)).strip()
            except Exception:
                文 = str(值).strip()
            return 文 if 文 in 名们 else ""

        for 键 in ("sname", "name", "server", "服名"):
            if 键 in 关键字:
                拎 = 认(关键字.get(键))
                if 拎:
                    return 拎
        for 值 in 参数们:
            if isinstance(值, dict):
                拎 = 认(值.get("sname"))
                if 拎:
                    return 拎
                路径 = 值.get("path_params")
                if isinstance(路径, dict):
                    拎 = 认(路径.get("sname"))
                    if 拎:
                        return 拎
                continue
            拎 = 认(值)
            if 拎:
                return 拎
            # 还可能是 request 对象
            拎 = 认(self._从请求拿服名(值) if not isinstance(值, str) else "")
            if 拎:
                return 拎
        return ""

    def _从请求拿服名(self, request: Any = None) -> str:
        """URL ?s=服名 或路径里带来的服名；拿不到就空。"""
        if request is None:
            return ""
        try:
            # quart / starlette / aiohttp 三家都能这么碰碰运气
            参数 = getattr(request, "args", None) or getattr(request, "query_params", None)
            if 参数 is not None:
                v = 参数.get("s")
                if v:
                    return str(v)
        except Exception:
            pass
        try:
            匹 = re.search(r"[?&]s=([^&]+)", str(request))
            if 匹:
                return 匹.group(1)
        except Exception:
            pass
        try:
            # 路径参数版：/<插件名>/summary/<sname>。AstrBot 把占位符塞进 path_params。
            路径 = getattr(request, "path_params", None)
            if isinstance(路径, dict):
                v = 路径.get("sname")
                if v:
                    return urllib.parse.unquote(str(v))
        except Exception:
            pass
        try:
            # 兜底：直接从 str(request) 里抠 /summary/<名字>（有的框架 request 转成字符串就带路径）
            匹 = re.search(r"/(?:summary|refresh|data)/([^/?&]+)", str(request))
            if 匹:
                return urllib.parse.unquote(匹.group(1))
        except Exception:
            pass
        return ""

    async def 看板接口(self, request: Any = None, **_):
        return _网页响应(json.dumps(self._算汇总(self._从请求拿服名(request)), ensure_ascii=False))

    async def 数据接口(self, *参数: Any, **关键字: Any):
        return _网页响应(
            json.dumps(self._算汇总(self._挑服名(参数, 关键字)), ensure_ascii=False)
        )

    async def 刷新接口(self, *参数: Any, **关键字: Any):
        """视图上那个「刷新」按钮按下去就走这儿：立刻采一次，再把新数据丢回去。

        采样失败也不抛，把错误塞进汇总里让页面自己显示，省得按一下红一片。
        """
        服名 = self._挑服名(参数, 关键字)
        服 = next((x for x in self.服们 if x["name"] == 服名), None) if 服名 else None
        if 服 is None:
            服 = self.服们[0] if self.服们 else None
        try:
            await self._采一次(服)
        except Exception as 错:
            名 = 服["name"] if 服 else "?"
            态 = self._状态.setdefault(名, self._新状态())
            态["错误"] = str(错)[:200]
            logger.warning("[在线看板] 手动刷新 %s 失败：%s", 名, 错)
        return _网页响应(json.dumps(self._算汇总(服["name"] if 服 else ""), ensure_ascii=False))

    async def 页面接口(self, **_):
        return _网页响应(_读页面())

    async def 自检接口(self, request: Any = None, **_):
        概览 = []
        for 服 in self.服们:
            态 = self._状态.get(服["name"], {})
            当前 = 态.get("当前") or {}
            概览.append({
                "名": 服["name"],
                "地址": 服["address"],
                "模式": 服.get("mode", "自动"),
                "人数": int(当前.get("c") or 0),
                "来源": 态.get("来源", ""),
                "错误": 态.get("错误", ""),
            })
        return _网页响应(
            json.dumps(
                {
                    "ok": True,
                    "插件": 插件名,
                    "时区": self.时区名,
                    "间隔": self.间隔,
                    "服务器数": len(self.服们),
                    "服务器": 概览,
                    "现在": _说时间(_现在()),
                },
                ensure_ascii=False,
            )
        )

    # ---------- 指令

    @_消息过滤()
    async def _指令兜底(self, event: AstrMessageEvent):
        """命令过滤器认不出来时的后手。

        真机上遇到过：面板里看板好好的，发 /在线 却一点反应都没有，消息直接
        流到 LLM 去，被当成聊天内容回了一句。原因是 command 过滤器在这个版本里
        没把处理器挂上。这里用「所有消息」过滤器自己认一遍文本，认出来就走同一条
        指令逻辑，然后吃掉这条消息不往下传。
        """
        if time.time() - self._指令时刻 < 3:
            return  # 正规通道刚处理过，别回两遍
        文 = str(getattr(event, "message_str", "") or "").strip()
        if not 文:
            return
        if not (re.search(r"[/／!！#＃]\s*在线", 文) or re.fullmatch(r"在线(\s+.*)?", 文)):
            return
        参数 = re.sub(r"^.*?在线", "", 文).strip()
        self._指令时刻 = time.time()
        try:
            async for 回 in self.在线指令(event, 参数):
                await event.send(回)
        except Exception as 错:
            logger.warning("[在线看板] 兜底指令出错：%s", 错)
            try:
                await event.send(event.plain_result("查的时候出了点岔子：%s" % 错))
            except Exception:
                pass
        try:
            event.stop_event()
        except Exception:
            pass


    @_deco("command", "在线")
    async def 在线指令(self, event: AstrMessageEvent, 参数: str = ""):
        self._指令时刻 = time.time()
        logger.info("[在线看板] 收到指令，参数=%r，共 %d 台服", 参数, len(self.服们))
        try:
            会话 = str(getattr(event, "unified_msg_origin", "") or "")
            if 会话:
                self._群们.add(会话)
        except Exception:
            pass
        参数 = (参数 or "").strip()
        服名 = ""
        if 参数 and 参数.split()[0] not in (
            "帮助", "help", "?", "排行", "榜", "时长", "热", "时段", "热力",
            "曲线", "line", "图", "today", "今天", "状态", "status", "体检",
        ):
            服名 = 参数.split()[0]
            参数 = 参数[len(服名):].strip()
        # 「曲线 工业」：关键词后头还跟着服名
        if 参数:
            头 = 参数.split()[0]
            if 头 in ("曲线", "line", "图") and len(参数.split()) > 1:
                服名 = 参数.split()[1]
                参数 = 头
        if 参数 in ("帮助", "help", "?"):
            yield event.plain_result(
                "/在线 —— 全部服概览\n"
                "/在线 服名 —— 看指定那台（多服时）\n"
                "/在线 排行 —— 今天玩得最久的\n"
                "/在线 热 —— 哪个时段最热闹" + chr(10) +
                "/在线 曲线 —— 近 24 小时多少人（画张图）" + chr(10) + "/在线 曲线 服名 —— 只看某一台" + chr(10) + "/在线 状态 —— 每台采到没采到"
            )
            return
        if not 服名 and len(self.服们) <= 1:
            服名 = self.服们[0]["name"] if self.服们 else ""
        if 服名 and 服名 not in [x["name"] for x in self.服们]:
            有 = "、".join(x["name"] for x in self.服们) or "（一台都没配）"
            yield event.plain_result("没有叫「%s」的服。现在盯着：%s" % (服名, 有))
            return
        数据 = self._算汇总(服名)
        if 参数 in ("曲线", "line", "图"):
            # 没说哪台：多服就把每台都画一张
            if not 服名 and len(self.服们) > 1:
                画了 = False
                for 台 in self.服们:
                    台数据 = self._算汇总(台["name"])
                    台图 = _画曲线卡(台数据)
                    if 台图 and await _发卡片图(event, 台图):
                        画了 = True
                if 画了:
                    return
            图bytes = _画曲线卡(数据)
            if 图bytes and await _发卡片图(event, 图bytes):
                return
            行 = []
            if 服名 or len(self.服们) <= 1:
                要看的 = [数据]
            else:
                要看的 = [self._算汇总(台["name"]) for 台 in self.服们]
            for 一台 in 要看的:
                点们 = 一台.get("近24") or []
                标 = ("【%s】" % 一台["服务器"]) if len(self.服们) > 1 else ""
                行.append(标 + "近 24 小时（每小时峰值）：")
                for i in range(0, len(点们), 6):
                    段 = 点们[i:i + 6]
                    行.append("  " + "  ".join("%s点 %d" % (q["点"], q["峰"]) for q in 段))
                行.append("最高 %d 人" % max([q["峰"] for q in 点们] + [0]))
            yield event.plain_result(chr(10).join(行))
            return
        if 参数 in ("状态", "status", "体检"):
            行 = ["采样状态："]
            for 台 in self.服们:
                态 = self._状态.get(台["name"], {})
                现 = 态.get("当前") or {}
                错 = 态.get("错误") or ""
                行.append(
                    "【%s】%s:%s %s" % (
                        台["name"], 台["address"], 台["game_port"],
                        ("连不上：" + 错) if 错 else ("在线 %d 人 · 来源 %s" % (int(现.get("c") or 0), 态.get("来源") or "?")),
                    )
                )
            yield event.plain_result(chr(10).join(行))
            return
        if 参数 in ("排行", "榜", "时长"):
            行 = ["今天玩得最久："]
            for i, 条 in enumerate(数据["排行今天"][:10], 1):
                行.append("%d. %s  %s" % (i, 条["名"], 条["说"]))
            if len(行) == 1:
                行.append("今天还没人有记录呢")
            yield event.plain_result("\n".join(行))
            return
        if 参数 in ("热", "时段", "热力"):
            格 = 数据["热力"]
            最热 = []
            for 周 in range(7):
                for 时 in range(24):
                    if 格[周][时] > 0:
                        最热.append((格[周][时], 周, 时))
            最热.sort(reverse=True)
            名 = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            行 = ["最热闹的时段："]
            for 值, 周, 时 in 最热[:5]:
                行.append("%s %02d:00 前后  平均 %.1f 人在线" % (名[周], 时, 值))
            if not 最热:
                行.append("还没攒够数据")
            yield event.plain_result("\n".join(行))
            return
        if 参数 in ("today", "今天") or 服名 or len(self.服们) == 1:
            # 详情：优先发卡片图（查服小助手那种），画不了退回文字
            图bytes = _画服卡(数据, 数据)
            if 图bytes and await _发卡片图(event, 图bytes):
                return
            现在 = 数据["现在"]
            今日 = 数据["今天"]
            标 = ("【%s】" % 数据["服务器"]) if len(self.服们) > 1 else ""
            谁们 = "、".join(现在["名单"])
            行 = [
                "%s现在 %d/%d 人在线%s"
                % (标, 现在["人数"], 现在["上限"], "：" + 谁们 if 谁们 else ""),
                "今天峰值 %d 人（%02d 点），来过 %d 人"
                % (今日["峰值"], 今日["峰值时"], 今日["来过"]),
                "看板已跑 %s" % (数据.get("运行") or "—"),
            ]
            if 数据["错误"]:
                行.append("（最近一次没连上：%s）" % 数据["错误"])
            yield event.plain_result("\n".join(行))
            return
        # 多服概览：每台一张卡，全画出来就不发文字了
        发了 = False
        for 服 in self.服们:
            d2 = self._算汇总(服["name"])
            图bytes = _画服卡(d2, d2)
            if 图bytes and await _发卡片图(event, 图bytes):
                发了 = True
        if 发了:
            return
        行 = []
        for 服 in self.服们:
            d = self._算汇总(服["name"])
            现 = d["现在"]
            今 = d["今天"]
            行.append(
                "【%s】%d/%d 人%s · 今天峰值 %d%s"
                % (
                    d["服务器"], 现["人数"], 现["上限"],
                    ("：" + "、".join(现["名单"])) if 现["名单"] else "",
                    今["峰值"],
                    ("（连不上：%s）" % d["错误"]) if d["错误"] else "",
                )
            )
        if not 行:
            行.append("还没配服务器。面板 → 插件设置里填 servers 或 address。")
        else:
            行.append("看板已跑 %s" % (self._算汇总(self.服们[0]["name"]).get("运行") or "—"))
        yield event.plain_result("\n".join(行))


# ---------------------------------------------------------------- 指令卡片图

_字体路径: Optional[str] = None
_字体找过 = False


def _找中文字体() -> str:
    """找一台机器上能用的中文字体。找不到返回空串（调用方就退回文字）。"""
    global _字体路径, _字体找过
    if _字体找过:
        return _字体路径 or ""
    _字体找过 = True
    候选 = [
        # Windows
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/Deng.ttf",
        # Linux 常见
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        # 插件自己带的（想省事就把字体丢这儿）
        str(PLUGIN_DIR / "fonts" / "msyh.ttc"),
        str(PLUGIN_DIR / "fonts" / "NotoSansCJK-Regular.ttc"),
    ]
    for 路 in 候选:
        try:
            if Path(路).is_file():
                _字体路径 = 路
                return 路
        except Exception:
            continue
    # 候选都没有就翻目录，只认名字里带中文字样样的
    关键词 = ("noto", "wqy", "msyh", "simhei", "simsun", "droid", "sourcehan",
              "source-han", "pingfang", "cjk", "uming", "ukai", "yahei")
    for 根 in ("/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts"),
               str(PLUGIN_DIR / "fonts")):
        try:
            for 个 in Path(根).rglob("*"):
                if 个.suffix.lower() not in (".ttc", ".ttf", ".otf"):
                    continue
                小名 = 个.name.lower()
                if any(词 in 小名 for 词 in 关键词):
                    _字体路径 = str(个)
                    return str(个)
        except Exception:
            continue
    _字体路径 = ""
    return ""


def _中文字体(尺寸: int):
    """按字号取字体；这台机器没中文字体就返回 None。"""
    from PIL import ImageFont
    路 = _找中文字体()
    if not 路:
        return None
    try:
        return ImageFont.truetype(路, 尺寸)
    except Exception:
        return None


def _画服卡(数据: Dict[str, Any], 现在们: Dict[str, Any]) -> Optional[bytes]:
    """把一台服的概况画成一张卡片图。画不了就 None，指令退回文字。"""
    try:
        from PIL import Image, ImageDraw  # noqa: F401
    except Exception:
        return None
    try:
        return _画服卡主体(数据, 现在们)
    except Exception as 错:
        logger.warning("[在线看板] 画卡片没画成，这条改用文字：%s", 错)
        return None


def _画服卡主体(数据: Dict[str, Any], 现在们: Dict[str, Any]) -> Optional[bytes]:
    from PIL import Image, ImageDraw
    import io as _io

    字 = _中文字体(26)
    字大 = _中文字体(46)
    字小 = _中文字体(19)
    字超 = _中文字体(72)
    字微 = _中文字体(16)
    if not (字 and 字大 and 字小 and 字超 and 字微):
        # 这台机器一个中文字体都没有，画出来全是豆腐块，不如直接发文字
        logger.info("[在线看板] 没有找到中文字体，卡片图跳过（发文字）")
        return None

    宽, 高 = 720, 430
    图 = Image.new("RGB", (宽, 高), (10, 16, 36))
    画 = ImageDraw.Draw(图)

    青 = (53, 208, 165)
    白 = (230, 238, 255)
    淡 = (140, 160, 200)

    画.rounded_rectangle([8, 8, 宽 - 8, 高 - 8], radius=24, outline=(70, 110, 190), width=2)

    服名 = 数据.get("服务器") or "服务器"
    现 = 现在们.get("现在", {})
    今 = 现在们.get("今天", {})
    在线 = 现.get("人数", 0)
    上限 = 现.get("上限", 0)
    峰值 = 今.get("峰值", 0)
    来过 = 今.get("来过", 0)

    画.text((36, 30), 服名, font=字大, fill=白)
    状态 = "在线" if not 数据.get("错误") else "离线"
    点色 = 青 if 状态 == "在线" else (220, 90, 90)
    画.ellipse([宽 - 130, 44, 宽 - 110, 64], fill=点色)
    画.text((宽 - 100, 40), 状态, font=字, fill=白)

    画.text((36, 104), str(在线), font=字超, fill=青)
    画.text((36 + 110, 150), "/ %d 人" % 上限, font=字, fill=淡)

    指标 = [("今日峰值", str(峰值) + " 人"), ("今天来过", str(来过) + " 人")]
    框宽 = (宽 - 72 - 14) // 2
    x = 36
    for 名, 值 in 指标:
        画.rounded_rectangle([x, 220, x + 框宽, 300], radius=14, fill=(20, 30, 58))
        画.text((x + 16, 232), 名, font=字小, fill=淡)
        画.text((x + 16, 260), str(值)[:14], font=字, fill=白)
        x += 框宽 + 14

    名单 = 现.get("名单") or []
    名单字 = "、".join(名单[:8]) if 名单 else "没人"
    if len(名单) > 8:
        名单字 += " 等 %d 人" % len(名单)
    行们 = _折行(画, "在线：" + 名单字, 字小, 宽 - 72)
    画行 = 318
    for 一 in 行们[:2]:
        画.text((36, 画行), 一, font=字小, fill=白)
        画行 += 26
    画.text((36, 画行 + 2), "看板已跑 " + (数据.get("运行") or "—"), font=字微, fill=(120, 150, 200))
    画.text((36, max(380, 画行 + 24)), "采样于 " + (数据.get("生成于") or "").split(" (")[0][:16], font=字微, fill=(90, 110, 150))

    输 = _io.BytesIO()
    图.save(输, format="PNG")
    return 输.getvalue()


def _画曲线卡(数据):
    """近 24 小时在线人数曲线。画不了返回 None，调用方退回文字。"""
    try:
        return _画曲线卡主体(数据)
    except Exception as 错:
        logger.warning("[在线看板] 曲线图没画成，改用文字：%s", 错)
        return None


def _画曲线卡主体(数据):
    from PIL import Image, ImageDraw
    import io as _io

    字 = _中文字体(24)
    字大 = _中文字体(34)
    字小 = _中文字体(15)
    if not (字 and 字大 and 字小):
        logger.info("[在线看板] 没有中文字体，曲线图跳过（发文字）")
        return None

    点们 = 数据.get("近24") or []
    if not 点们:
        return None

    宽, 高 = 720, 430
    图 = Image.new("RGB", (宽, 高), (10, 16, 36))
    画 = ImageDraw.Draw(图)

    青 = (53, 208, 165)
    白 = (230, 238, 255)
    淡 = (140, 160, 200)
    线 = (40, 56, 92)

    画.rounded_rectangle([8, 8, 宽 - 8, 高 - 8], radius=24, outline=(70, 110, 190), width=2)
    画.text((36, 30), (数据.get("服务器") or "服务器") + " · 近 24 小时", font=字大, fill=白)

    左, 右, 上, 下 = 56, 宽 - 40, 96, 高 - 74
    峰 = max([p["峰"] for p in 点们] + [1])
    刻度 = 4
    for i in range(刻度 + 1):
        y = 下 - (下 - 上) * i / 刻度
        画.line([左, y, 右, y], fill=线, width=1)
        画.text((左 - 34, y - 8), str(int(峰 * i / 刻度)), font=字小, fill=淡)

    步 = (右 - 左) / max(len(点们) - 1, 1)
    坐标 = []
    for i, p in enumerate(点们):
        x = 左 + 步 * i
        y = 下 - (下 - 上) * (p["峰"] / 峰)
        坐标.append((x, y))

    if len(坐标) >= 2:
        面 = 坐标 + [(右, 下), (左, 下)]
        面图 = Image.new("RGBA", (宽, 高), (0, 0, 0, 0))
        面画 = ImageDraw.Draw(面图)
        面画.polygon(面, fill=(53, 208, 165, 46))
        图.paste(面图, (0, 0), 面图)

    for i in range(len(坐标) - 1):
        画.line([坐标[i], 坐标[i + 1]], fill=青, width=3)
    for i, (x, y) in enumerate(坐标):
        if 点们[i]["有"] and 点们[i]["峰"] > 0:
            画.ellipse([x - 4, y - 4, x + 4, y + 4], fill=青)
        if i % 4 == 0:
            画.text((x - 14, 下 + 8), 点们[i]["点"], font=字小, fill=淡)

    最高 = max(点们, key=lambda p: p["峰"])
    画.text(
        (左, 下 + 34),
        "最高 %d 人（%s 点）· 每小时取峰值 · 采样于 %s"
        % (最高["峰"], 最高["点"], (数据.get("生成于") or "").split(" (")[0][:16]),
        font=字小,
        fill=淡,
    )

    输 = _io.BytesIO()
    图.save(输, format="PNG")
    return 输.getvalue()


async def _发卡片图(event: AstrMessageEvent, 图bytes: bytes):
    """把 PNG 发出去；Image 组件哪一家都试一下。"""
    import tempfile as _tempfile
    try:
        from astrbot.api.message_components import Image as 图组件
        from astrbot.core.message.message_event_result import MessageChain
    except Exception:  # 离线测试没 astrbot，画了也发不出去
        return False
    文件 = _tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    文件.write(图bytes)
    文件.close()
    try:
        组件 = 图组件.fromFileSystem(文件.name)
    except Exception:
        组件 = 图组件(file=文件.name)
    try:
        await event.send(MessageChain(chain=[组件]))
        return True
    except Exception as 错:
        # 发不出去就别装死：让调用方退回文字版
        logger.warning("[在线看板] 卡片图没发出去：%s", 错)
        return False


# ---------------------------------------------------------------- 网页小工具


def _网页响应(文本: str) -> Any:
    """不同版本 AstrBot 的 Web 框架不一样，挨个试。"""
    try:
        from quart import Response as 四分响应

        return 四分响应(文本, mimetype="application/json")
    except Exception:
        pass
    try:
        from aiohttp import web as 网页

        return 网页.Response(text=文本, content_type="application/json")
    except Exception:
        pass
    try:
        from starlette.responses import Response as 星响应

        return 星响应(文本, media_type="application/json")
    except Exception:
        return 文本


def _读页面() -> str:
    for 位置 in (
        Path(PLUGIN_DIR) / "views" / "在线看板" / "index.html",
        Path(PLUGIN_DIR) / "view.html",
    ):
        try:
            if 位置.exists():
                return 位置.read_text(encoding="utf-8")
        except Exception:
            continue
    return "<h1>页面文件没找到</h1>"


def _读服务器们(取配置):
    """从配置里读出要盯的服务器列表。

    三种填法都认：
    1. servers 填 JSON 数组：[{"name":"生存服","address":"mc.x.com",...},...]
    2. servers 一行一台（逗号分隔）：生存服,mc.x.com,25565
    3. 什么都不填，退回旧的单服字段（address/game_port/...），凑一台。
    名字没给就按地址当名字；同名撞了加序号。
    """
    服们 = []

    def 一台(名, 地址, 端口=25565, 模式="自动",
             查询端口=0, rcon端口=25575, rcon密码=""):
        地址 = str(地址 or "").strip()
        if not 地址:
            return {}
        名 = str(名 or "").strip() or 地址
        return {
            "name": 名,
            "address": 地址,
            "game_port": _整数(端口, 25565) or 25565,
            "mode": str(模式 or "自动"),
            "query_port": _整数(查询端口, 0) or 0,
            "rcon_port": _整数(rcon端口, 25575) or 25575,
            "rcon_password": str(rcon密码 or ""),
        }

    def 收一个(项):
        if isinstance(项, dict):
            服 = 一台(项.get("name"), 项.get("address") or 项.get("addr"),
                      项.get("game_port", 25565), 项.get("mode", "自动"),
                      项.get("query_port", 0), 项.get("rcon_port", 25575),
                      项.get("rcon_password", ""))
        elif isinstance(项, str) and 项.strip():
            段 = [x.strip() for x in 项.split(",")]
            if len(段) < 2:
                return
            服 = 一台(段[0], 段[1], 段[2] if len(段) > 2 else 25565)
        else:
            return
        if 服:
            服们.append(服)

    # 分格优先：面板上这一格一格填的才是用户现在的意思
    if not 服们:
        for 序, 后缀 in ((1, ""), (2, "2"), (3, "3")):
            一 = 一台(
                取配置("name" + 后缀, ""), 取配置("address" + 后缀, ""),
                取配置("game_port" + 后缀, 25565), 取配置("mode" + 后缀, "自动"),
                取配置("query_port" + 后缀, 0), 取配置("rcon_port" + 后缀, 25575),
                取配置("rcon_password" + 后缀, ""),
            )
            if 一:
                服们.append(一)

    # 分格一台都没填，才回头读老配置里的 servers 文本（兼容以前填过的人）
    if not 服们:
        原文 = 取配置("servers", "") or ""
        if isinstance(原文, (list, tuple)):
            for 项 in 原文:
                收一个(项)
        elif str(原文).strip():
            文 = str(原文).strip()
            try:
                解 = json.loads(文)
                if isinstance(解, list):
                    for 项 in 解:
                        收一个(项)
                else:
                    收一个(解)
            except Exception:
                for 行行 in 文.splitlines():
                    行行 = 行行.strip()
                    if 行行:
                        收一个(行行)

    # 名字去重
    见过 = {}
    定稿 = []
    for 服 in 服们:
        名 = 服["name"]
        if 名 in 见过:
            见过[名] += 1
            服["name"] = "%s-%d" % (名, 见过[名])
        else:
            见过[名] = 1
        定稿.append(服)
    return 定稿


def _数据目录() -> Path:
    根 = None
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        根 = Path(get_astrbot_plugin_data_path())
    except Exception:
        根 = None
    if 根 is None:
        根 = Path(PLUGIN_DIR) / "data"
    目录 = 根 / 插件名
    try:
        目录.mkdir(parents=True, exist_ok=True)
        return 目录
    except Exception:
        兜底 = Path(PLUGIN_DIR) / "data"
        兜底.mkdir(parents=True, exist_ok=True)
        return 兜底
