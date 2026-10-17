"""时段窗口边界：分钟精度、越界拒绝。不调模型，不写库，可反复跑。

这条用例守住的是用例 17 的判断口径：条件给的是「周六上午 10:00-12:00 才能出门」
时，方案落在窗口内算用上条件，越界算没用上。判定必须精确到分钟——
抹掉分钟会把 12:30 当成 12:00，越界方案反而通过；倒序和零时长
（22:00-08:00、10:00-10:00）也不是可用时段，放行会让窗口判断失效。

运行：/tmp/hack_venv/bin/python tests/test_window_boundary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 被测函数就在验收脚本里，直接取来用，不复制一份：复制两份必然漂移，
# 改了实现忘了改测试，边界就等于没守。
from tests.test_e2e_chain import _clock_minutes, _within_window

results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


WIN_START = 10 * 60
WIN_END = 12 * 60


def case(name, text, expect):
    clock = _clock_minutes(text)
    got = _within_window(clock, WIN_START, WIN_END)
    check(name, got is expect, (text, clock, got, expect))


# 落在窗口内：完整覆盖窗口、只占一部分、贴着端点都算合规。
case("10:00-12:00 完整覆盖窗口", "周六上午 10:00-12:00", True)
case("10:00-11:30 落在窗口内", "周六上午 10:00-11:30", True)
case("10:30-11:00 占窗口一部分", "周六上午 10:30-11:00", True)
case("10:00-12:00 用中文冒号", "周六上午 10：00-12：00", True)
case("10:00-12:00 用破折号", "周六上午 10:00—12:00", True)
case("10 点到 12 点整点写法", "周六上午 10 点到 12 点", True)
# 越界：早一分钟开始、晚一分钟结束都拒绝。
case("09:59 开始早一分钟拒绝", "周六上午 09:59-12:00", False)
case("12:01 结束晚一分钟拒绝", "周六上午 10:00-12:01", False)
case("14:00 排在下午拒绝", "周六下午 14:00-16:00", False)
case("09:00-10:30 整个在窗口前拒绝", "周六上午 09:00-10:30", False)
case("11:30-13:00 跨到午后拒绝", "周六上午 11:30-13:00", False)
# 12:30 曾被小时口径误判成 12:00 放行，这里明确拒绝。
case("10:00-12:30 超出半小时拒绝", "周六上午 10:00-12:30", False)
# 不可用时段。
case("倒序时段拒绝", "周六上午 12:00-10:00", False)
case("零时长拒绝", "周六上午 10:00-10:00", False)
case("跨夜写法拒绝", "22:00-08:00", False)
# 取不到时段就不该通过：不能因为读不出时间就默认合规。
case("没有时段文本拒绝", "周六上午一起出门", False)
case("只有一个钟点拒绝", "周六上午 10 点", False)


def main():
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(("[PASS] " if ok else "[FAIL] ") + name)
        if not ok:
            print("       " + repr(detail))
    print("\n通过 %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
