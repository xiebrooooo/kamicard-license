"""跨语言一致性验证：签发器(纯 JS) ↔ 校验器(Python) 必须字节级一致。

这是整套卡密系统**最容易被改坏**的地方：手机版签发器是纯 JS 重写的同一套
算法，手工改字段布局时极容易只改一边，结果「手机签的卡软件不认」，
而且这种 bug 在小批量测试时不容易发现。

跑法：
    python tests/test_parity.py

它会：
  1. 用固定 nonce + 固定时间，在 JS 侧签发一张卡
  2. 用同样的参数在 Python 侧签发一张卡
  3. 比对两张卡是否**完全相同**
  4. 再用 Python 解析 JS 签出来的卡（证明能互认）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
import shutil
import struct
import subprocess
import sys

_PKG = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))

import license as LIC  # noqa: E402

MACHINE = "89b6a98b26c5628f"
HOURS = 24
NOW = 1800000000
NONCE = b"\x12\x34"


def find_node() -> str | None:
    """找一个能用的 node。优先托管版，方便直接跑。"""
    cands: list[str] = []
    base = pathlib.Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if base.is_dir():
        for d in sorted(base.iterdir(), reverse=True):
            exe = d / "node.exe"
            if exe.exists():
                cands.append(str(exe))
    found = shutil.which("node")
    if found:
        cands.append(found)
    return cands[0] if cands else None


def py_card() -> str:
    """Python 侧用同样的参数签发一张卡。"""
    exp = NOW + HOURS * 3600
    fp = hashlib.sha256(MACHINE.encode()).digest()[:4]
    body = struct.pack(">I4s2sH", exp, fp, NONCE, 0x0001)
    sig = hmac.new(LIC._KEY, body, hashlib.sha256).digest()[:4]
    chk = sum(body + sig) & 0xFF
    raw = base64.b32encode(body + sig + bytes([chk])).decode().rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def main() -> int:
    node = find_node()
    if not node:
        print("找不到 node，跳过 JS 一致性验证（这只是可选检查）")
        return 0

    js_script = pathlib.Path(__file__).parent / "test_js_parity.js"
    print(f"node: {node}")
    print(f"跑 {js_script.name} ...")
    r = subprocess.run([node, str(js_script)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        print("JS 侧执行失败：")
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        return 1

    info = json.loads(r.stdout.strip().splitlines()[-1])
    js_card = info["card"]
    py = py_card()

    print(f"\n  JS  签发: {js_card}")
    print(f"  Py  签发: {py}")

    fails = 0
    if js_card == py:
        print("\n  OK  两边签发结果完全一致")
    else:
        fails += 1
        print("\n  XX  两边结果不一致！")
        print("       说明有一边的密钥、字段顺序或时间基准被改过了。")
        # 逐字节指出差异位置，方便定位
        for i, (a, b) in enumerate(zip(js_card, py)):
            if a != b:
                print(f"       第一个不同在第 {i} 个字符: JS={a!r} Py={b!r}")
                break

    # 互认：Python 要能解析 JS 签出来的卡
    parsed = LIC.parse_card(js_card)
    if parsed.get("status") == LIC.OK:
        print("  OK  Python 能解析 JS 签的卡（可互认）")
    else:
        fails += 1
        print(f"  XX  Python 解析不了 JS 的卡: {parsed}")

    if parsed.get("machine_fp") == LIC._fingerprint(MACHINE).hex():
        print("  OK  机器指纹一致")
    else:
        fails += 1
        print(f"  XX  机器指纹不一致: {parsed.get('machine_fp')}")

    if parsed.get("expires_at") == NOW + HOURS * 3600:
        print("  OK  到期时刻一致")
    else:
        fails += 1
        print(f"  XX  到期时刻不一致: {parsed.get('expires_at')} "
              f"!= {NOW + HOURS * 3600}")

    print(f"\n{'=' * 56}")
    print("  一致性验证通过 ✓" if not fails else f"  有 {fails} 项不一致")
    print("=" * 56)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
