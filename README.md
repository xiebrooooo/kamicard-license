# 卡密系统

离线卡密 + 机器绑定。不联网、不要服务器，一个 `license.py` 搞定签发和校验。

纯标准库零依赖，可直接接进任何 Python / pywebview 桌面软件。

> ## ⚠️ 安全提示：本仓库公开，签发密钥也随之公开
>
> `issuer/` 里含**主签发密钥**，本仓库是 public，所以**任何看到这个仓库的人
> 都能离线造出无限张有效卡密**。
>
> 这是仓库 owner 的明确选择（当技术演示用）。如果你打算真的靠卡密卖软件，
> 先跑 `python tools/rotate_key.py` 换密钥，再把仓库转私有 ——
> 详见 **[安全说明.md](安全说明.md)**。

---

## 快速开始

```bash
git clone <本仓库地址>
cd 卡密系统-独立版

# 自检（52 项，不用联网）
python tests/test_license.py

# 跨语言一致性验证（JS 签发器 ↔ Python 校验器）
python tests/test_parity.py

# 打开签发工具
python issuer/card_tool.py

# 或者：把 issuer/ui/card_tool_mobile.html 用浏览器打开，手机也能签卡
```

---

## 30 秒看懂结构

```
license.py              核心算法（纯标准库，零依赖）—— 客户端和签发端共用同一份
client/
  activate_ui.html      可直接复用的激活弹窗（自包含单文件，无前端依赖）
issuer/
  card_tool.py          签发工具（桌面 GUI）
  ui/card_tool.html     签发工具界面
  ui/card_tool_mobile.html  单文件手机版签发器（丢手机浏览器里就能签）
examples/
  integrate_example.py  最小接入示例（能跑）
tests/
  test_license.py       逻辑自检 52 项
  test_parity.py        跨语言一致性验证（JS 签发器 ↔ Python 校验器）
  test_js_parity.js     上面那个的 JS 侧
tools/
  rotate_key.py         一键轮换签发密钥（同时改两个文件 + 自动验证）
build_issuer.bat        一键把签发工具打成 exe
卡密签发手册.txt         签发操作手册
接入指南.md              怎么接进你自己的软件
安全说明.md              ⚠ 本仓库公开，密钥已泄露，先看这个
```

**两个角色，别搞混：**

| 文件 | 给谁 | 能不能外发 |
|---|---|---|
| `license.py` | 要被保护的软件（客户那里） | 可以，但密钥会随之落到客户端 |
| `issuer/` 整个目录 | 你自己 | 内含签发密钥 |
| `client/activate_ui.html` | 要被保护的软件 | 可以 |

---

## 三步接入

```python
import pathlib, sys
sys.path.insert(0, r"卡密系统-独立版")
import license as LIC

APP_ROOT = pathlib.Path(__file__).resolve().parent   # license.json 存这
LIC.set_root(APP_ROOT)

# 1. 查状态
st = LIC.check(APP_ROOT)
if st["status"] != LIC.OK:
    print("需要激活，本机指纹：", LIC.machine_id())
    print("剩余：", st.get("remaining_text", ""))

# 2. 激活（用户把卡密给你）
ok, msg = LIC.activate(APP_ROOT, "XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX")
print(ok, msg)

# 3. 运行时巡检（放在业务循环 / 线程里）
if not LIC.sentinel(APP_ROOT):
    print("环境异常")
```

带界面的话，把 `client/activate_ui.html` 塞进 pywebview，后端只要暴露
`get_lic()` 和 `activate(card)` 两个方法 —— 照抄 `examples/integrate_example.py`。

详细接入说明（含防绕过建议、踩坑）见 **接入指南.md**。

---

## 验证一下没坏

```bash
python tests/test_license.py     # 52 项逻辑检查
python tests/test_parity.py      # JS 签发器 ↔ Python 校验器 是否字节级一致
```

`test_parity.py` 别看它小，它拦的是最容易出的事故：有人改了
`card_tool_mobile.html` 里的密钥或字段顺序，只改了一边 ——
结果手机签的卡软件全都不认，而且不测根本发现不了。

---

## 轮换密钥

密钥同时存在于两个文件里，手工改容易只改一边。用这个脚本：

```bash
python tools/rotate_key.py            # 生成随机新密钥
python tools/rotate_key.py --dry-run  # 只预览，不落盘
python tools/rotate_key.py --key aabbccddeeff0011223344556677   # 指定密钥
```

它会同时改写 `license.py` 的 `_KEY` 和 `card_tool_mobile.html` 的 `KEY` 数组，
然后自动跑一遍一致性验证。

⚠ 轮换后**已发出的卡密全部失效**，老用户需要重新签发。详见 [安全说明.md](安全说明.md)。

---

## 签发卡密

**方式 A（桌面）**：`python issuer/card_tool.py`
**方式 B（手机）**：把 `issuer/ui/card_tool_mobile.html` 用浏览器打开，
存成书签或发到手机，随时随地能签
**方式 C（命令行）**：

```python
import license as LIC
print(LIC.make_card(24, "用户的16位指纹"))   # 24 小时卡
```

三种方式签出来的卡**完全通用**，因为算法是同一套（已用 `test_parity.py` 验证）。

---

## 设计要点

| 特性 | 怎么实现的 |
|---|---|
| 完全离线 | 到期时刻和机器指纹都内嵌在卡密里，不需要服务器 |
| 改不了 | HMAC-SHA256 签名，改一个字符就作废 |
| 换机无效 | 卡密里存目标机器指纹哈希，别台机器激活直接拒 |
| 一卡一次 | 用过的卡哈希记进 license.json，重复激活被拒 |
| 时长叠加 | 续期 = max(现在, 旧到期) + 新卡时长 |
| 存档改不了 | license.json 里的到期时间也被签名覆盖 |
| 不暴露"授权" | 校验点分散 + `sentinel()`/`health()` 模糊返回，没有 `if not licensed` 可搜 |

**能挡住的**：改卡密日期、复制别人的 license.json、用别台机器的卡、
搜一行代码注释掉。

**挡不住的**：反编译 exe 找密钥（**本仓库更是直接把密钥写在明面上**）；
把系统时间往回调（纯离线方案的固有弱点）。

密钥在 `license.py` 的 `_KEY`（拆成 7 段拼接）和
`card_tool_mobile.html` 的 `KEY` 数组里 —— 用 `tools/rotate_key.py` 一次改两处。

**纯离线方案的固有局限**：密钥必然躺在客户端里，反编译一定能找到。
要更强的保护，要么上 pyarmor 正式授权，要么改成在线校验
（卡密只存 ID，客户端联网验签）。

---

## 关于打包加密

`license.py` 才 16KB 左右，可以顺手上 pyarmor 加密再打进 exe。

注意 **pyarmor 试用版对单个 .py 有体积上限**（实测 30KB 以内比较稳）。
超了会报 `ERROR out of license`，而且**不会让整个命令失败** ——
它会把那个文件原样拷贝过去，只在日志里写一句
`is not obfuscated because out of license`，不留意就以为加密成功了。

单文件快速排查：

```bash
pyarmor gen -O /tmp/t1 license.py
```

⚠ 别运行 `pyarmor-7.exe --version` / `pyarmor-8.exe --version` 这类旧版入口 ——
可能会创建并覆盖 `~/.pyarmor/license.lic`。
