"""一键轮换签发密钥。

为什么要这个脚本：
    密钥同时存在于两个地方，手工改极容易只改一边 ——
    结果手机签的卡软件全不认，而且小批量测试发现不了：
      · license.py              的 _KEY（拆成 7 段拼接）
      · issuer/ui/card_tool_mobile.html 的 KEY 数组

它做的事：
    1. 生成一把新的 14 字节随机密钥
    2. 同时改写上面两个文件
    3. 跑 tests/test_parity.py 确认两边仍然字节级一致

⚠ 轮换后**所有已发出的卡密立即失效**，老用户必须重新签发。
   已激活的用户不受影响（他们的 license.json 里存的是到期时刻，不含卡密本身），
   但存档签名也依赖这把密钥 —— 所以老用户的 license.json 会变成 corrupt，
   需要重新激活。轮换前想清楚。

跑法：
    python tools/rotate_key.py            # 生成随机新密钥
    python tools/rotate_key.py --dry-run  # 只看会改成什么，不落盘
"""

from __future__ import annotations

import argparse
import pathlib
import re
import secrets
import subprocess
import sys

PKG = pathlib.Path(__file__).resolve().parent.parent
LICENSE_PY = PKG / "license.py"
MOBILE_HTML = PKG / "issuer" / "ui" / "card_tool_mobile.html"

KEY_LEN = 14           # 与现有密钥长度一致
GROUP = 2              # 每 2 字节一组拼接，和原代码风格保持一致


def current_key_py() -> bytes:
    """从 license.py 里把当前密钥还原出来。"""
    src = LICENSE_PY.read_text(encoding="utf-8")
    m = re.search(r"_KEY\s*=\s*\(([^)]*)\)", src, re.S)
    if not m:
        raise SystemExit("在 license.py 里找不到 _KEY = ( ... ) 的写法")
    hexes = re.findall(r"b\"((?:\\x[0-9a-fA-F]{2})+)\"", m.group(1))
    if not hexes:
        # 兼容没转义的单行写法
        hexes = re.findall(r"b\"(.*?)\"", m.group(1))
        out = b"".join(h.encode("latin1", "ignore") for h in hexes)
    else:
        out = b"".join(bytes.fromhex(h.replace("\\x", "")) for h in hexes)
    return out


def render_py_key(key: bytes) -> str:
    """生成 _KEY = (...) 那一段的文本，保持 2 字节一组、带换行的原风格。"""
    groups = [key[i:i + GROUP] for i in range(0, len(key), GROUP)]
    lines = []
    for i, g in enumerate(groups):
        esc = "".join(f"\\x{b:02x}" for b in g)
        tail = " +" if i < len(groups) - 1 else ""
        lines.append(f'    b"{esc}"{tail}')
    return "_KEY = (\n" + "\n".join(lines) + "\n)"


def render_js_key(key: bytes) -> str:
    """生成 JS 端 const KEY = new Uint8Array([...]) 那一段。"""
    groups = [key[i:i + GROUP] for i in range(0, len(key), GROUP)]
    parts = []
    for g in groups:
        parts.append(",".join(f"0x{b:02x}" for b in g))
    body = ",\n  ".join(parts)
    return f"const KEY = new Uint8Array([\n  {body}\n]);"


def rotate(new_key: bytes, dry: bool = False) -> int:
    # 注意：这里必须用**函数式替换**（lambda），不能直接把字符串交给 re.subn。
    # 替换串里的 \x 会被 re 当成转义序列解析，直接报
    # "bad escape \x at position ..."。这是踩过的坑。
    repl_py = lambda _m: render_py_key(new_key)      # noqa: E731

    # ---- license.py ----
    py_src = LICENSE_PY.read_text(encoding="utf-8")
    py_new, n_py = re.subn(r"_KEY\s*=\s*\([^)]*\)", repl_py, py_src, count=1, flags=re.S)
    if n_py != 1:
        print("XX  license.py 里的 _KEY 没被替换，请检查写法")
        return 1

    # ---- card_tool_mobile.html ----
    js_src = MOBILE_HTML.read_text(encoding="utf-8")
    js_new, n_js = re.subn(
        r"const KEY = new Uint8Array\(\[[\s\S]*?\]\);",
        lambda _m: render_js_key(new_key), js_src, count=1)
    if n_js != 1:
        print("XX  card_tool_mobile.html 里的 KEY 数组没被替换")
        return 1

    print(f"新密钥（{len(new_key)} 字节）: {new_key.hex()}")
    if dry:
        print("\n--- license.py 会改成 ---")
        print(render_py_key(new_key))
        print("\n--- card_tool_mobile.html 会改成 ---")
        print(render_js_key(new_key))
        print("\n（--dry-run，没有落盘）")
        return 0

    LICENSE_PY.write_text(py_new, encoding="utf-8")
    MOBILE_HTML.write_text(js_new, encoding="utf-8")
    print(f"✓ 已改写 {LICENSE_PY.name} 与 {MOBILE_HTML.name}")

    # ---- 立刻验证两边一致 ----
    print("\n跑 tests/test_parity.py 验证一致性 ...")
    r = subprocess.run(
        [sys.executable, str(PKG / "tests" / "test_parity.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout.strip()[-900:])
    if r.returncode != 0:
        print("\nXX  一致性验证没通过，两边可能没改对，请检查上面的输出")
        return 1

    print("\n" + "=" * 56)
    print("  密钥轮换完成")
    print("=" * 56)
    print("  ⚠ 已发出的旧卡密全部失效，需要重新签发")
    print("  ⚠ 老用户的 license.json 会变成 corrupt，需重新激活")
    print("  ⚠ 记得重新打包签发工具和你的软件")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="轮换卡密签发密钥")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不落盘")
    ap.add_argument("--key", help="指定新密钥（hex，28 个字符＝14 字节）")
    args = ap.parse_args()

    if not LICENSE_PY.exists() or not MOBILE_HTML.exists():
        print("XX  找不到 license.py 或 card_tool_mobile.html，请在包根目录下运行")
        return 1

    old = current_key_py()
    print(f"当前密钥（{len(old)} 字节）: {old.hex()}")

    if args.key:
        try:
            new_key = bytes.fromhex(args.key.strip())
        except ValueError:
            print("XX  --key 要传十六进制，例如 aabbccddeeff0011223344556677")
            return 1
        if len(new_key) != KEY_LEN:
            print(f"XX  密钥必须是 {KEY_LEN} 字节（{KEY_LEN*2} 个十六进制字符），"
                  f"你给的是 {len(new_key)} 字节")
            return 1
    else:
        new_key = secrets.token_bytes(KEY_LEN)

    if new_key == old:
        print("新密钥和旧的一样，重新生成一次")
        new_key = secrets.token_bytes(KEY_LEN)

    return rotate(new_key, dry=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
