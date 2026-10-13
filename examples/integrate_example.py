"""最小接入示例 —— 把卡密系统塞进你自己的软件，照抄这个文件就行。

跑法：
    python examples/integrate_example.py

它做的事：
  1. 用 pywebview 起一个窗口，加载 client/activate_ui.html
  2. 后端暴露 get_lic / activate 两个方法给界面
  3. 演示「三个校验点」该放在哪里（启动前 / 主循环里 / 关键功能里）

想直接看效果又不想开窗口？跑 tests/test_license.py，那是纯逻辑验证。
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

_PKG = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))

import webview  # noqa: E402

import license as LIC  # noqa: E402

# 你的程序目录 —— license.json 就存在这里
APP_ROOT = pathlib.Path(__file__).resolve().parent / "_demo_data"
APP_ROOT.mkdir(parents=True, exist_ok=True)
LIC.set_root(APP_ROOT)


class Api:
    """暴露给界面的方法。名字要和 activate_ui.html 里调用的一致。"""

    def __init__(self) -> None:
        self.activated = False

    # ---- activate_ui.html 需要的两个方法 ----
    def get_lic(self) -> dict:
        st = LIC.check(APP_ROOT)
        return {
            "status": st.get("status"),
            "remaining": st.get("remaining", 0),
            "remaining_text": st.get("remaining_text", ""),
            "expires_at": st.get("expires_at", 0),
            "machine": LIC.machine_id(),
        }

    def activate(self, card: str) -> dict:
        ok, msg = LIC.activate(APP_ROOT, (card or "").strip())
        if ok:
            self.activated = True
            # 商品的关键状态在这里刷新；把"已授权"这个布尔值散进业务字段，
            # 比留一个 self.licensed = True 更难被一搜就改
            self.sync_feature_state()
        return {"ok": ok, "msg": msg, "lic": self.get_lic()}

    # ---- 业务侧的三个校验点（这才是真正决定"能不能用"的地方）----

    def sync_feature_state(self) -> None:
        """校验点 ①：启动时 / 激活成功后，把状态同步到业务变量。

        刻意写成读取一个模糊指标，而不是 if not licensed: exit() ——
        后者反编译后一搜就能定位并注释掉。
        """
        self.engine_ready = LIC.health() >= 1.0

    def run_main_task(self) -> bool:
        """校验点 ②：每次干正事之前。"""
        if not LIC.sentinel(APP_ROOT):
            # 不说"未授权"，只说环境异常 —— 引导用户去激活，而不是提示改哪
            print("[环境] 运行环境发生变化，功能已暂停")
            return False
        print("[环境] 正常，开始处理 ...")
        return True


def demo() -> None:
    api = Api()
    api.sync_feature_state()

    st = LIC.check(APP_ROOT)
    print("=" * 56)
    print("  本机指纹 :", LIC.machine_id())
    print("  当前状态 :", st.get("status"), st.get("remaining_text", ""))
    print("=" * 56)

    if st.get("status") != LIC.OK:
        print("\n还没激活。给你自己签一张测试卡（24 小时）：")
        card = LIC.make_card(24, LIC.machine_id())
        print("  卡密:", card)
        print("  按回车用这张卡激活 ...")
        try:
            input()
        except EOFError:
            pass
        ok, msg = LIC.activate(APP_ROOT, card)
        print(" ", "✓" if ok else "×", msg)
        st = LIC.check(APP_ROOT)
        print("  激活后状态:", st.get("status"), st.get("remaining_text", ""))

    print("\n跑 3 次业务循环（校验点 ②）:")
    for _ in range(3):
        api.run_main_task()
        time.sleep(0.2)

    print("\n打开激活窗界面（pywebview）...")
    html = _PKG / "client" / "activate_ui.html"
    webview.create_window("接入示例", url=str(html), js_api=api,
                          width=760, height=600, background_color="#1C1C1B")
    webview.start(debug=False)


class BackgroundWorker:
    """校验点 ③：常驻线程里也要定期查 —— 让绕过的人得改多处，而不是一处。"""

    def __init__(self, root: pathlib.Path, interval: float = 20.0) -> None:
        self.root = root
        self.interval = interval
        self._stop = threading.Event()
        self._fail = 0

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if not LIC.sentinel(self.root):
                self._fail += 1
                if self._fail >= 3:
                    print("[环境] 连续异常，停止工作线程")
                    return


if __name__ == "__main__":
    demo()
