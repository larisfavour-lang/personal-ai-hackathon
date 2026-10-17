"""手机地址选择：真实 Wi-Fi 优先，VPN 隧道地址不能冒充。

这一轮用户扫码打不开，根因是地址取错了：原实现用
socket.connect(192.0.2.1) 取默认路由的出口地址，VPN 开着时默认路由指向
utun*，于是给出 10.141.39.25 这种隧道地址。地址格式完全正常，手机和电脑
却不在同一物理网络上，扫码、手打都打不开，用户只会以为自己输错了。

这里用 ifconfig 的 fixture 驱动真实函数，不联网、不改系统配置：
  - active 的 en0 要排在最前；
  - utun*/awdl*/lo* 的地址一律不出现；
  - 只有虚拟接口时明确说明取不到可信地址，而不是给一个打不开的；
  - PA_LAN_IP 手工指定优先。

运行：/tmp/hack_venv/bin/python tests/lan_address_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


# 两份 fixture：一份是「Wi-Fi + VPN 同时在」的真实形态（本机当前形态），
# 一份是「只有 VPN」，用来验「取不到可信地址时不要瞎给」。
FIXTURE_WIFI_VPN = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500
\tinet 10.35.23.83 netmask 0xffffff00 broadcast 10.35.23.255
awdl0: flags=8943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX> mtu 1500
utun4: flags=8051<UP,POINTPOINT,RUNNING,MULTICAST> mtu 1500
\tinet 10.251.1.1 --> 10.251.1.1 netmask 0xffffffff
utun5: flags=8051<UP,POINTPOINT,RUNNING,MULTICAST> mtu 1200
\tinet 10.141.39.25 --> 10.141.39.25 netmask 0xffffff00
"""

FIXTURE_VPN_ONLY = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
utun5: flags=8051<UP,POINTPOINT,RUNNING,MULTICAST> mtu 1200
\tinet 10.141.39.25 --> 10.141.39.25 netmask 0xffffff00
"""

# 以太网在跑、Wi-Fi 也在但没连线：active 的那个要排在前面。
FIXTURE_ETH_ACTIVE = """en0: flags=8863<UP,BROADCAST,MULTICAST> mtu 1500
\tinet 192.168.1.50 netmask 0xffffff00
en5: flags=8963<UP,BROADCAST,SMART,RUNNING,PROMISC,SIMPLEX> mtu 1500
\tinet 10.0.0.7 netmask 0xffffff00
"""


def run_with_fixture(text, port=8000):
    """把 ifconfig 的输出换成 fixture，跑真实的 _lan_addresses。"""
    import subprocess
    os.environ["PA_DATA_DIR"] = tempfile.mkdtemp(prefix="pa_lan_")
    os.environ.pop("PA_LAN_IP", None)
    sys.path.insert(0, str(ROOT))
    from app import main as app_main

    class R:
        stdout = text
        returncode = 0

    orig = subprocess.run
    subprocess.run = lambda *a, **k: R()
    try:
        # 兜底那条（默认路由）也要一起挡掉，否则 fixture 之外的真实隧道地址会混进来。
        import socket
        orig_socket = socket.socket
        class FakeSock:
            def __init__(self, *a, **k): pass
            def settimeout(self, t): pass
            def connect(self, addr): raise OSError("fixture: no route")
            def getsockname(self): return ("10.141.39.25", 0)
            def close(self): pass
        socket.socket = FakeSock
        try:
            return app_main._lan_addresses(port)
        finally:
            socket.socket = orig_socket
    finally:
        subprocess.run = orig


def main() -> int:
    urls = run_with_fixture(FIXTURE_WIFI_VPN)
    check("Wi-Fi 与 VPN 同时在时，真实 Wi-Fi 地址排在第一位",
          urls and urls[0] == "http://10.35.23.83:8000", {"urls": urls})
    check("VPN 隧道地址（utun5）不出现在列表里",
          all("10.141.39.25" not in u for u in urls), {"urls": urls})
    check("AirDrop（awdl0）地址不出现",
          all("awdl" not in u for u in urls), {"urls": urls})

    urls_vpn = run_with_fixture(FIXTURE_VPN_ONLY)
    check("只有 VPN 接口时不给打不开的地址（宁缺勿错）",
          urls_vpn == [], {"urls": urls_vpn})

    urls_eth = run_with_fixture(FIXTURE_ETH_ACTIVE)
    check("active 的以太网优先于未连线的 Wi-Fi",
          urls_eth and urls_eth[0] == "http://10.0.0.7:8000", {"urls": urls_eth})
    check("未连线的 Wi-Fi 地址仍可作为备选给出",
          any("192.168.1.50" in u for u in urls_eth), {"urls": urls_eth})

    # 手工指定优先：用户明确知道该用哪个地址时不受探测影响。
    os.environ["PA_LAN_IP"] = "10.35.23.83"
    sys.path.insert(0, str(ROOT))
    import importlib
    from app import main as app_main
    importlib.reload(app_main)
    urls_manual = app_main._lan_addresses(8000)
    os.environ.pop("PA_LAN_IP", None)
    check("PA_LAN_IP 手工指定优先",
          urls_manual and urls_manual[0] == "http://10.35.23.83:8000",
          {"urls": urls_manual})

    _report()
    return 0 if all(ok for _, ok, _ in results) else 1


def _report():
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 66)
    for name, ok, detail in results:
        print("%s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            print("      证据：%s" % detail)
    print("=" * 66)
    print("手机地址选择：%d/%d 通过" % (passed, len(results)))
    print("边界：本脚本用 ifconfig fixture 驱动真实函数，不联网、不改网络配置；")
    print("      真机上能否打开仍取决于手机与电脑是否真的在同一局域网。")


if __name__ == "__main__":
    sys.exit(main())
