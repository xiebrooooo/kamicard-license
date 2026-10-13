"""卡密系统核心模块 —— 离线卡密 + 机器绑定，纯标准库，零依赖。

一个文件同时承担两个角色：
  · **客户端**（打包进要卖的软件里）：machine_id / activate / check / sentinel
  · **签发端**（自己留着，别外发）：make_card

卡密的二进制结构（共 17 字节，base32 后是 24 个字符）：

    偏移   长度   含义
    0      4      到期时刻 —— Unix 时间戳，秒级（uint32 大端）
    4      4      机器指纹 —— 目标机器码 SHA-256 的前 4 字节
    8      2      随机盐   —— 防止同规格卡生成出一模一样的字符串
    10     2      功能位   —— uint16，bit0=正式版（预留）
    12     4      签名     —— HMAC-SHA256(密钥, 前 12 字节) 的前 4 字节
    16     1      校验和   —— sum(body+sig) & 0xFF，用于区分「手输错」和「被篡改」

为什么这么设计：
  · 卡密自带到期时间和机器指纹 → **完全离线**，不需要服务器
  · HMAC 签名 → 改一个字符就作废，没密钥的人伪造不出新卡
  · 绑定机器指纹 → 一张卡只能在一台电脑上激活

代码里几个刻意的选择（都是为了增加改包成本）：
  · 密钥拆成 7 段在运行时拼接，直接搜字符串搜不到完整密钥
  · 不在启动处写 `if not valid: exit()` —— 那种一行代码就能定位并注释掉
  · 对外不暴露"授权无效"这个概念，失败只体现为别的功能异常（sentinel/health）
  · 签名比较用 hmac.compare_digest，避免通过耗时差异反推

————————————————————————————————————————————————————————
接入别的软件：见 接入指南.md，最快三步 ——

    import license as LIC
    LIC.set_root(pathlib.Path(__file__).parent)   # license.json 存哪
    LIC.check(LIC.ROOT_HOLDER[0])                 # 拿到 {status, remaining_text}
————————————————————————————————————————————————————————
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import platform
import struct
import subprocess
import time
from typing import Optional

_EPOCH = 1577836800          # 2020-01-01 00:00:00 UTC
_KEY = (
    b"\x6b\x1f" + b"\xa3\x4d" + b"\x0e\x72" +
    b"\xc9\x18" + b"\x54\xbe" + b"\x37\xf1" + b"\x8a\x25"
)
_LICENSE_FILE = "license.json"

# 各种状态码。前端/主程序只看这些，不直接暴露"授权"两个字。
OK = "ok"
MISSING = "missing"          # 还没激活过
EXPIRED = "expired"          # 到期
WRONG_MACHINE = "wrong_machine"
CORRUPT = "corrupt"          # 卡密本身格式不对或被改过
BIND_PENDING = "bind_pending"  # 卡有效，但还没绑到本机


# ---------------------------------------------------------------- 机器码

def _machine_guid() -> str:
    """Windows 安装 ID（注册表 MachineGuid）。

    这是**最稳定**的机器标识 —— 只有重装系统才会变，
    不受网卡增减、硬盘更换、主机改名影响，而且读注册表是毫秒级的，
    不用起子进程（不像 wmic 那样在 Win11 上直接没了）。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            v, _ = winreg.QueryValueEx(k, "MachineGuid")
            return str(v).strip()
    except Exception:
        return ""


def _disk_serial() -> str:
    """取系统盘序列号 —— 比 MAC 稳定（网卡换了也不变）。

    注意：wmic 在 Windows 11 上已被移除，所以主要靠 CIM 那条；
    两条都拿不到就返回空串，交给上层用别的依据。
    """
    cmds = (
        # Windows 10 及以前
        'wmic diskdrive where "DeviceID=\'C:\'" get SerialNumber',
        # Windows 11：用 CIM 查物理盘（C: 是分区，要按分区别名反查）
        'pw' + 'ershell -NoProfile -Command '
        '"(Get-Partition -DriveLetter C | Get-Disk).SerialNumber"',
    )
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, timeout=4)
            if r.returncode != 0:
                continue
            txt = r.stdout.decode("utf-8", errors="ignore").strip()
            for line in txt.splitlines():
                s = line.strip()
                if s and s.lower() != "serialnumber" and len(s) >= 6:
                    return s
        except Exception:
            continue
    return ""


_MID_CACHE: str = ""


def machine_id() -> str:
    """本机指纹，16 位十六进制。

    取值依据（按稳定性排序）：
      1. Windows MachineGuid —— 重装系统才变
      2. 系统盘序列号       —— 换硬盘才变
      3. 主机名             —— 改名才变

    **刻意不用 MAC 地址**：`uuid.getnode()` 在拿不到有效网卡时会返回
    一个随机数（第 41 位为 1），导致同一台机器每次算出来的指纹都不一样，
    用户手里的卡会莫名其妙失效。这个坑踩过。

    结果缓存在模块级 —— 取磁盘序列号要起子进程（可能各超时几秒），
    而 check()/activate()/sentinel() 会频繁调用它，不缓存会拖慢启动。
    """
    global _MID_CACHE
    if _MID_CACHE:
        return _MID_CACHE

    parts = []
    guid = _machine_guid()
    if guid:
        parts.append("guid:" + guid)
    disk = _disk_serial()
    if disk:
        parts.append("disk:" + disk)
    parts.append("node:" + platform.node())

    raw = "|".join(parts).encode("utf-8", errors="ignore")
    _MID_CACHE = hashlib.sha256(raw).hexdigest()[:16]
    return _MID_CACHE


def _fingerprint(mid: str) -> bytes:
    return hashlib.sha256(mid.encode()).digest()[:4]


# ---------------------------------------------------------------- 卡密编解码

def _b32(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=")


def _unb32(text: str) -> Optional[bytes]:
    t = text.replace("-", "").replace(" ", "").upper()
    pad = "=" * ((8 - len(t) % 8) % 8)
    try:
        return base64.b32decode(t + pad)
    except Exception:
        return None


def make_card(hours: int, machine: str) -> str:
    """生成一张卡密。**签发端用**。

    流程（按店主的设计）：
      1. 用户打开软件，界面上会显示他那台电脑的指纹（16 位十六进制）
      2. 用户把指纹复制发给店主
      3. 店主按「指纹 + 有效时长」生成这张卡 —— **支持按小时签发**
      4. 卡密里**内嵌了目标电脑的指纹**，拿到别的电脑上就是废纸

    hours   —— 有效时长（小时），1 = 一小时卡，720 = 一个月
    machine —— **必填**，目标电脑的指纹（不是店主自己的！）
    """
    h = int(hours)
    if not 1 <= h <= 87600:          # 上限 10 年
        raise ValueError("有效时长需在 1 ~ 87600 小时之间（最多 10 年）")
    if not machine or not isinstance(machine, str):
        raise ValueError("必须提供目标电脑的指纹")

    # 到期时间用**秒**存（uint32），精度到秒才能支持小时卡；
    # uint32 上限 2106 年，离现在还有 79 年余量
    exp = int(time.time()) + h * 3600
    fp = _fingerprint(machine.strip().lower())
    # 随机盐：没有它的话，同一批生成的同规格卡会是**完全相同的字符串**
    nonce = os.urandom(2)
    body = struct.pack(">I4s2sH", exp, fp, nonce, 0x0001)
    sig = hmac.new(_KEY, body, hashlib.sha256).digest()[:4]
    chk = sum(body + sig) & 0xFF
    raw = _b32(body + sig + bytes([chk]))
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def parse_card(card: str) -> dict:
    """解析并验签卡密，返回 {status, expires_at, machine_fp, features}。"""
    raw = _unb32(card or "")
    if not raw or len(raw) != 17:
        return {"status": CORRUPT}
    body, sig, chk = raw[:12], raw[12:16], raw[16]

    # 先看校验和 —— 用户手输错位时能立刻给出更准确的提示
    if (sum(body + sig) & 0xFF) != chk:
        return {"status": CORRUPT}

    want = hmac.new(_KEY, body, hashlib.sha256).digest()[:4]
    # 恒定时间比较，避免通过耗时差异反推
    if not hmac.compare_digest(sig, want):
        return {"status": CORRUPT}

    exp, fp, _nonce, features = struct.unpack(">I4s2sH", body)
    return {
        "status": OK,
        # exp 是到期时刻的秒级时间戳
        "expires_at": exp,
        "machine_fp": "" if fp == b"\x00\x00\x00\x00" else fp.hex(),
        "features": features,
    }


def remaining_text(seconds: int) -> str:
    """把剩余秒数格式化成「X 天 Y 小时」这种给用户看的写法。

    超过 1 天显示天+小时；不足 1 天只显示小时；不足 1 小时显示分钟。
    """
    seconds = max(0, int(seconds))
    d, r = divmod(seconds, 86400)
    h, r2 = divmod(r, 3600)
    m = r2 // 60
    parts = []
    if d:
        parts.append(f"{d}天")
    if h:
        parts.append(f"{h}小时")
    if not d and m:
        parts.append(f"{m}分钟")
    return " ".join(parts) if parts else "不足 1 分钟"


def remaining_seconds(card: str) -> int:
    """卡密还剩多少秒。解析失败时返回 -1。"""
    info = parse_card(card)
    if info.get("status") != OK:
        return -1
    return max(0, int(info["expires_at"] - time.time()))


# ---------------------------------------------------------------- 本地存档

def _store_path(root: pathlib.Path) -> pathlib.Path:
    return root / _LICENSE_FILE


def read_store(root: pathlib.Path) -> dict:
    p = _store_path(root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_store(root: pathlib.Path, data: dict) -> bool:
    try:
        _store_path(root).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _card_key(card: str) -> str:
    """卡密指纹，用来记录「这张卡已经用过了」。存哈希不存明文。"""
    return hashlib.sha256(
        card.replace("-", "").replace(" ", "").upper().encode()
    ).hexdigest()[:16]


def _store_sig(machine: str, expires_at: int, used: list) -> str:
    """给本地存档签名。

    存档里的 expires_at 是明文 JSON，不加签名的话用户直接改成 2099 年就白嫖了。
    签名覆盖 机器指纹 + 到期时刻 + 已用卡列表，任何一项被改都会对不上。
    """
    payload = f"{machine}|{int(expires_at)}|{','.join(sorted(used))}".encode()
    return hmac.new(_KEY, payload, hashlib.sha256).hexdigest()[:20]


def activate(root: pathlib.Path, card: str) -> tuple[bool, str]:
    """用卡密激活本机。返回 (是否成功, 给用户看的提示)。

    两条规则：
      · **时长叠加** —— 新到期 = max(现在, 旧到期) + 这张卡的时长。
        所以先用 1 小时卡、再补一张 1 天卡，总时长是 1 天 1 小时。
      · **一卡一次** —— 用过的卡密哈希记在存档里，同一张卡第二次输入会被拒。
    """
    card = (card or "").strip().upper()

    info = parse_card(card)
    st = info.get("status")

    if st == CORRUPT:
        return False, "卡密格式不对，请检查有没有输错或漏了字符"
    if st != OK:
        return False, "卡密无效"

    mid = machine_id()
    # 卡密里内嵌的指纹必须和本机一致 —— 这就是「别的电脑完全无法使用」的保证
    if info["machine_fp"] != _fingerprint(mid).hex():
        return False, "这张卡不是这台电脑的（指纹不匹配）"

    now = int(time.time())
    if info["expires_at"] <= now:
        return False, "这张卡已经过期了"

    store = read_store(root)
    used = list(store.get("used") or [])

    h = _card_key(card)
    if h in used:
        return False, "这张卡已经用过了（一张卡只能激活一次）"

    # 时长叠加。
    # 「这张卡的时长」用 卡的到期时刻 - 现在 来算 —— 相当于假定「签发后立刻就激活了」。
    # 正常流程（店主签完马上发给你）误差只有几分钟；要是把卡囤了半个月才用，
    # 那这半个月会从卡时长里扣掉，这是这个近似的代价。
    duration = int(info["expires_at"] - now)
    old_exp = int(store.get("expires_at") or 0)
    base = max(now, old_exp)          # 旧卡还没用完就从旧到期接着算，用完了就从现在算
    new_exp = base + duration

    used.append(h)
    data = {
        "card": card,
        "machine": mid,
        "activated_at": int(store.get("activated_at") or now),
        "last_activated_at": now,
        "expires_at": new_exp,
        "features": info.get("features", 1),
        "used": used,
    }
    data["sig"] = _store_sig(mid, new_exp, used)

    if not write_store(root, data):
        return False, "保存失败，检查文件夹是否可写"

    left = remaining_text(new_exp - now)
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(new_exp))
    if old_exp > now:
        return True, f"续期成功，叠加后共剩 {left}（至 {until}）"
    return True, f"激活成功，剩余 {left}（至 {until}）"


def check(root: pathlib.Path) -> dict:
    """检查本机授权。返回 {status, remaining, remaining_text, expires_at}。"""
    data = read_store(root)
    if not data.get("card"):
        return {"status": MISSING, "remaining": 0}

    info = parse_card(data["card"])
    if info.get("status") != OK:
        return {"status": CORRUPT, "remaining": 0}

    mid = machine_id()
    # 存档里的机器码要和当前机器一致 —— 防止把 license.json 拷到别的电脑
    if data.get("machine") != mid:
        return {"status": WRONG_MACHINE, "remaining": 0}

    # 以**存档里**的到期时刻为准（它包含叠加出来的时长），但要验签 ——
    # 否则用户把 expires_at 改成 2099 就能永久白嫖
    used = list(data.get("used") or [])
    exp = int(data.get("expires_at") or 0)
    if not exp or data.get("sig") != _store_sig(mid, exp, used):
        return {"status": CORRUPT, "remaining": 0}

    now = time.time()
    if exp <= now:
        return {"status": EXPIRED, "remaining": 0, "expires_at": exp}

    rem = int(exp - now)
    return {
        "status": OK,
        "remaining": rem,
        "remaining_text": remaining_text(rem),
        "days_left": rem // 86400,                # 兼容旧字段
        "expires_at": exp,                        # 叠加后的实际到期时刻
        "features": data.get("features", 1),
    }


# ---------------------------------------------------------------- 内部哨兵

# 校验失败不在启动时立刻发作，而是累计到阈值后由调用方决定。
# 这样想绕过的人没法靠搜一句 `if not licensed` 就改掉。
_TALLY = {"n": 0, "last": 0.0}


def sentinel(root: pathlib.Path, every: float = 20.0) -> bool:
    """各处线程每隔一会儿调一次；返回 True 表示当前看起来一切正常。

    正常时几乎零开销（只比较时间戳）。
    """
    now = time.time()
    if now - _TALLY["last"] < every:
        return True
    _TALLY["last"] = now
    st = check(root).get("status")
    if st == OK:
        _TALLY["n"] = 0
        return True
    _TALLY["n"] += 1
    return _TALLY["n"] < 3      # 容忍偶发失败，连续 3 次才判定为异常


def health() -> float:
    """给主程序用的一个模糊指标：1.0 最健康。

    不直接返回 True/False，避免调用方写出 `if health(): exit()` 这种一搜就中的代码。
    """
    st = check(ROOT_HOLDER[0]).get("status") if ROOT_HOLDER[0] else CORRUPT
    if st == OK:
        return 1.0
    return 0.0 if st in (EXPIRED, WRONG_MACHINE) else 0.5


ROOT_HOLDER = [pathlib.Path(".")]


def set_root(root: pathlib.Path) -> None:
    ROOT_HOLDER[0] = root


if __name__ == "__main__":  # 调试用：签发一张卡并当场校验
    import pathlib as _pl
    import sys as _sys

    _root = _pl.Path(_sys.argv[1]) if len(_sys.argv) > 1 else _pl.Path(".")
    set_root(_root)
    mid = machine_id()
    print("本机指纹 :", mid)
    card = make_card(24, machine=mid)
    print("测试卡密 :", card)
    print("解析结果 :", parse_card(card))
    print("剩余     :", remaining_text(remaining_seconds(card)))
    print("校验     :", check(_root))
