"""卡密系统自检 —— 不连网、不开窗口，纯逻辑验证。

跑法：
    python tests/test_license.py

覆盖：
  签发 / 解析 / 激活 / 状态查询 / 时限叠加 / 一卡一次
  换机拒绝 / 过期拒绝 / 篡改卡密拒绝 / 篡改存档拒绝 / 手输错识别
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
import shutil
import struct
import sys
import tempfile
import time

_PKG = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))

import license as LIC  # noqa: E402

pass_n = 0
fail_n = 0


def check(name: str, got, want) -> None:
    global pass_n, fail_n
    if got == want:
        pass_n += 1
        print(f"  OK  {name}")
    else:
        fail_n += 1
        print(f"  XX  {name}\n        实际: {got!r}\n        期望: {want!r}")


def truthy(name: str, got) -> None:
    check(name, bool(got), True)


class Tmp:
    """每个用例一个干净目录，互不干扰。"""

    def __init__(self) -> None:
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="lic_test_"))

    def __enter__(self) -> pathlib.Path:
        return self.path

    def __exit__(self, *a) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def raw_card(exp: int, machine: str, features: int = 1, nonce: bytes = b"\x00\x01") -> str:
    """手工构造一张卡（可以造出过期卡这类 make_card 不让你签的）。"""
    fp = hashlib.sha256(machine.strip().lower().encode()).digest()[:4]
    body = struct.pack(">I4s2sH", exp, fp, nonce, features)
    sig = hmac.new(LIC._KEY, body, hashlib.sha256).digest()[:4]
    chk = sum(body + sig) & 0xFF
    b32 = base64.b32encode(body + sig + bytes([chk])).decode().rstrip("=")
    return "-".join(b32[i:i + 4] for i in range(0, len(b32), 4))


def main() -> int:
    mid = LIC.machine_id()
    print(f"本机指纹: {mid}\n")

    # ============ [1] 签发与解析 ============
    print("[1] 签发 / 解析")
    card = LIC.make_card(24, mid)
    print(f"  · 卡密样例: {card}")
    info = LIC.parse_card(card)
    check("解析状态 = ok", info.get("status"), LIC.OK)
    truthy("卡密里带机器指纹", info.get("machine_fp"))
    check("指纹与目标机器一致",
          info.get("machine_fp"), LIC._fingerprint(mid).hex())
    truthy("功能位存在", info.get("features") is not None)

    dt = abs(info["expires_at"] - (time.time() + 24 * 3600))
    truthy("到期时刻 ≈ 现在 + 24 小时", dt < 5)

    # 同一台机器 + 同一时长，两次生成必须不同（靠随机盐）
    c1, c2 = LIC.make_card(24, mid), LIC.make_card(24, mid)
    truthy("两次签发结果不同（随机盐生效）", c1 != c2)

    check("非法时长被拒（下面两条）", True, True)
    try:
        LIC.make_card(0, mid)
        check("时长 0 应报错", "没报错", "报错")
    except ValueError:
        check("时长 0 应报错", "报错", "报错")
    try:
        LIC.make_card(24, "")
        check("不给指纹应报错", "没报错", "报错")
    except ValueError:
        check("不给指纹应报错", "报错", "报错")

    # ============ [2] 完整激活流程 ============
    print("\n[2] 激活 → 查状态")
    with Tmp() as root:
        st = LIC.check(root)
        check("没激活时状态 = missing", st.get("status"), LIC.MISSING)

        ok, msg = LIC.activate(root, card)
        truthy("激活成功", ok)
        print(f"  · 提示: {msg}")

        st = LIC.check(root)
        check("激活后状态 = ok", st.get("status"), LIC.OK)
        truthy("剩余时间文本非空", st.get("remaining_text"))
        check("剩余 ≈ 24 小时（误差 < 10s）",
              abs(st["remaining"] - 24 * 3600) < 10, True)
        print(f"  · 剩余: {st['remaining_text']}")

        # 存档里必须有签名，且不含明文密钥
        blob = (root / "license.json").read_text(encoding="utf-8")
        data = json.loads(blob)
        truthy("存档有 sig 字段", data.get("sig"))
        truthy("存档记录了机器码", data.get("machine") == mid)
        truthy("存档不含密钥明文", "6b1f" not in blob.lower())

    # ============ [3] 一卡一次 ============
    print("\n[3] 一卡只能用一次")
    with Tmp() as root:
        c = LIC.make_card(24, mid)
        ok1, _ = LIC.activate(root, c)
        ok2, msg2 = LIC.activate(root, c)
        truthy("第一次激活成功", ok1)
        check("第二次激活被拒", ok2, False)
        truthy("提示说明是重复使用", "用过" in msg2)
        print(f"  · 提示: {msg2}")

    # ============ [4] 时长叠加 ============
    print("\n[4] 续期叠加（1 小时卡 + 24 小时卡 = 25 小时）")
    with Tmp() as root:
        LIC.activate(root, LIC.make_card(1, mid))
        before = LIC.check(root)["remaining"]
        ok, msg = LIC.activate(root, LIC.make_card(24, mid))
        after = LIC.check(root)["remaining"]
        truthy("续期成功", ok)
        print(f"  · 提示: {msg}")
        truthy("续期后剩余 ≈ 25 小时",
               abs(after - (25 * 3600)) < 15)
        truthy("剩余时间确实变长了", after > before)

    # ============ [5] 换机拒绝 ============
    print("\n[5] 换机 / 换指纹")
    with Tmp() as root:
        other = "0123456789abcdef"
        c_other = LIC.make_card(24, other)
        ok, msg = LIC.activate(root, c_other)
        check("别的机器的卡激活失败", ok, False)
        truthy("提示是指纹不匹配", "指纹" in msg)
        print(f"  · 提示: {msg}")

        # 把自己的 license.json 拷到"别的机器"（改 machine 字段 + 重签）
        LIC.activate(root, LIC.make_card(24, mid))
        p = root / "license.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["machine"] = other
        d["sig"] = LIC._store_sig(other, d["expires_at"], d["used"])
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        check("存档机器码不对 → wrong_machine",
              LIC.check(root).get("status"), LIC.WRONG_MACHINE)

    # ============ [6] 过期 ============
    print("\n[6] 过期")
    with Tmp() as root:
        past = raw_card(int(time.time()) - 3600, mid)
        check("过期卡解析仍然合法（签名对）",
              LIC.parse_card(past).get("status"), LIC.OK)
        ok, msg = LIC.activate(root, past)
        check("过期卡激活失败", ok, False)
        truthy("提示是已过期", "过期" in msg)
        print(f"  · 提示: {msg}")

        # 已激活但时间到期 → expired
        LIC.activate(root, LIC.make_card(1, mid))
        p = root / "license.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["expires_at"] = int(time.time()) - 10          # 改成过去
        d["sig"] = LIC._store_sig(mid, d["expires_at"], d["used"])  # 重签才不被识别为篡改
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        check("到期后状态 = expired", LIC.check(root).get("status"), LIC.EXPIRED)

    # ============ [7] 篡改 ============
    print("\n[7] 篡改")
    good = LIC.make_card(24, mid)
    # 7-1 改一个字符
    i = len(good) // 2
    swapped = "A" if good[i] not in "A" else "B"
    bad = good[:i] + swapped + good[i + 1:]
    check("改一个字符 → 无效", LIC.parse_card(bad).get("status"), LIC.CORRUPT)

    # 7-2 砍掉一个字符（手输漏字）
    check("少一个字符 → 无效", LIC.parse_card(good[:-1]).get("status"), LIC.CORRUPT)

    # 7-3 空 / 乱码
    check("空字符串 → 无效", LIC.parse_card("").get("status"), LIC.CORRUPT)
    check("乱码 → 无效", LIC.parse_card("不是卡密").get("status"), LIC.CORRUPT)

    # 7-4 把到期时间改到 2099 年（不用密钥 → 签名对不上）
    body = bytearray(base64.b32decode(
        good.replace("-", "") + "=" * ((8 - len(good.replace("-", "")) % 8) % 8)))
    body[0:4] = struct.pack(">I", 4070908800)          # 2099-01-01
    tampered = base64.b32encode(bytes(body)).decode().rstrip("=")
    tampered = "-".join(tampered[i:i + 4] for i in range(0, len(tampered), 4))
    check("把卡密日期改到 2099 → 无效",
          LIC.parse_card(tampered).get("status"), LIC.CORRUPT)

    # 7-5 改存档里的 expires_at，但不重签 → corrupt
    with Tmp() as root:
        LIC.activate(root, LIC.make_card(24, mid))
        p = root / "license.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["expires_at"] = 4070908800                    # 2099
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        check("把存档改到 2099（不重签）→ corrupt",
              LIC.check(root).get("status"), LIC.CORRUPT)

        # 连 used 列表一起改也拦得住
        d2 = json.loads((root / "license.json").read_text(encoding="utf-8"))
        d2["expires_at"] = 4070908800
        d2["used"] = []
        (root / "license.json").write_text(
            json.dumps(d2, ensure_ascii=False), encoding="utf-8")
        check("连 used 一起改 → 仍然 corrupt",
              LIC.check(root).get("status"), LIC.CORRUPT)

    # ============ [8] 容错与辅助函数 ============
    print("\n[8] 容错 / 辅助")
    check("大小写与连字符都兼容",
          LIC.parse_card(good.lower().replace("-", "")).get("status"), LIC.OK)
    check("空格也兼容",
          LIC.parse_card(" " + good.replace("-", " ") + " ").get("status"), LIC.OK)

    check("remaining_text: 1 天 2 小时", LIC.remaining_text(86400 + 7200), "1天 2小时")
    check("remaining_text: 3 小时", LIC.remaining_text(3 * 3600), "3小时")
    check("remaining_text: 25 分钟", LIC.remaining_text(25 * 60), "25分钟")
    check("remaining_text: 0 → 不足 1 分钟", LIC.remaining_text(0), "不足 1 分钟")

    check("remaining_seconds 无效卡 = -1", LIC.remaining_seconds("xxx"), -1)
    truthy("remaining_seconds 有效卡 > 0", LIC.remaining_seconds(good) > 0)

    # 存档损坏不能让程序崩
    with Tmp() as root:
        (root / "license.json").write_text("{ 这不是 json", encoding="utf-8")
        check("存档损坏 → 当作未激活，不抛异常",
              LIC.check(root).get("status"), LIC.MISSING)

    # health() 的模糊取值
    with Tmp() as root:
        LIC.set_root(root)
        truthy("未激活时 health < 1.0", LIC.health() < 1.0)
        LIC.activate(root, LIC.make_card(24, mid))
        check("激活后 health = 1.0", LIC.health(), 1.0)

    # sentinel：连续失败要攒够次数才报警
    # 注意 every 是节流窗口 —— 用 every=0.0 才能让每次调用都真的去查，
    # 否则连着的几次调用会被窗口挡住，直接返回 True。
    with Tmp() as root:
        LIC._TALLY["n"] = 0
        LIC._TALLY["last"] = 0.0
        first = LIC.sentinel(root, every=0.0)
        second = LIC.sentinel(root, every=0.0)
        third = LIC.sentinel(root, every=0.0)
        truthy("单次异常不立即报警（容忍抖动）", first and second)
        check("连续 3 次才判定异常", third, False)

        # 正常时几乎零开销、且不累计
        with Tmp() as r2:
            LIC._TALLY["n"] = 0
            LIC._TALLY["last"] = 0.0
            LIC.activate(r2, LIC.make_card(24, mid))
            check("已激活时 sentinel 一直为真",
                  all(LIC.sentinel(r2, every=0.0) for _ in range(5)), True)
            check("已激活后计数被清零", LIC._TALLY["n"], 0)

    print(f"\n{'=' * 56}\n  通过 {pass_n}，失败 {fail_n}\n{'=' * 56}")
    return 1 if fail_n else 0


if __name__ == "__main__":
    sys.exit(main())
