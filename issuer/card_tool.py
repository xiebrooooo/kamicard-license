"""卡密签发工具（图形界面版，双击即用）。

⚠ 这个工具里含有**签发密钥**，只能自己留着，绝不能发给用户，
   也不要拷进要交给用户的软件目录 —— 给谁就等于把整套卡密系统送人了。

使用流程：
  1. 用户打开你卖出去的软件 → 界面显示他那台电脑的指纹 → 复制发给你
  2. 你打开这个工具，粘贴指纹，选有效期
  3. 点【生成卡密】→ 复制结果发回给用户
  4. 用户把卡密粘进软件激活 —— 别的电脑拿到这张卡也是废纸

单独运行（开发时）：
    python issuer/card_tool.py

打包成 exe：
    见上一级目录的 build_issuer.bat
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

# ---- 让本文件既能被 python 直接跑，也能被 PyInstaller 打包后在任意目录跑 ----
# license.py 在上一级目录（签发端和客户端共用同一份，避免两边算法漂移）
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

import webview  # noqa: E402

from license import (OK, make_card, parse_card,  # noqa: E402
                     remaining_seconds, remaining_text)


def _work_dir() -> pathlib.Path:
    """可写工作目录：打包后是 exe 所在目录，开发时是 issuer/。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return _HERE


def _bundle_dir() -> pathlib.Path:
    """只读资源目录：打包后是 _MEIPASS，开发时是 issuer/。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(getattr(sys, "_MEIPASS"))
    return _HERE


def _resource(*parts: str) -> pathlib.Path:
    """定位资源文件。先找内嵌资源，再退回工作目录（方便自己换界面）。"""
    p = _bundle_dir().joinpath(*parts)
    if p.exists():
        return p
    return _work_dir().joinpath(*parts)


ROOT = _work_dir()
CARDS_DIR = ROOT / "cards"
HISTORY_FILE = CARDS_DIR / "history.json"


class CardApi:
    """暴露给签发界面的接口（window.pywebview.api.xxx）。"""

    def __init__(self) -> None:
        CARDS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 签发

    def gen(self, machine: str, hours, note: str = "") -> dict:
        """按「指纹 + 时长（小时）」生成一张卡密。"""
        machine = (machine or "").strip().lower()
        if not machine:
            return {"ok": False, "msg": "请先粘贴用户发来的机器指纹"}
        if len(machine) != 16 or any(c not in "0123456789abcdef" for c in machine):
            return {"ok": False, "msg": "指纹格式不对：应该是 16 位十六进制"
                                       "（数字 0-9 和字母 a-f），请让用户重新复制"}

        try:
            hours = int(hours)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "时长必须是整数（小时）"}

        if not 1 <= hours <= 87600:
            return {"ok": False, "msg": "时长需在 1 ~ 87600 小时之间（最多 10 年）"}

        try:
            card = make_card(hours, machine)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"生成失败：{e}"}

        expires = time.strftime("%Y-%m-%d %H:%M",
                                time.localtime(time.time() + hours * 3600))

        # 记进历史，方便对账
        self._append_history({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "card": card,
            "machine": machine,
            "hours": hours,
            "expires": expires,
            "note": (note or "").strip(),
        })

        return {"ok": True, "card": card, "hours": hours,
                "remaining_text": remaining_text(hours * 3600),
                "expires": expires, "machine": machine}

    def check(self, card: str) -> dict:
        """查一张卡的详情 —— 用户反馈「卡用不了」时用它定位原因。"""
        card = (card or "").strip()
        info = parse_card(card)
        if info.get("status") != OK:
            return {"ok": False, "msg": "卡密无效（格式不对或被篡改过）"}
        rem = remaining_seconds(card)
        return {
            "ok": True,
            "expires": time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(info["expires_at"])),
            "remaining_text": remaining_text(rem),
            "machine_fp": info["machine_fp"],
            "expired": rem <= 0,
        }

    def history(self) -> dict:
        return {"items": self._load_history()[-100:]}

    def machine(self) -> dict:
        """本机指纹 —— 想给自己签一张测试卡时用。"""
        from license import machine_id
        return {"machine": machine_id()}

    # ------------------------------------------------------------ 内部

    def _load_history(self) -> list:
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _append_history(self, item: dict) -> None:
        items = self._load_history()
        items.append(item)
        HISTORY_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    api = CardApi()
    html = _resource("ui", "card_tool.html")
    webview.create_window(
        "卡密签发工具",
        url=str(html),
        js_api=api,
        width=720,
        height=560,
        min_size=(620, 480),
        background_color="#1C1C1B",
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
