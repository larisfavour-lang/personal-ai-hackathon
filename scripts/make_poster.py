"""生成参赛海报。文案与 README、mermaid 画板同一套，改内容只改这里。

用法：.venv/bin/python scripts/make_poster.py
输出：docs/delivery/poster.png

设计沿用既有视觉：暖白底 #F7F6F3，深绿 #34463F 作强调与底栏，
标题用 Songti SC Bold（衬线），正文用 Heiti SC Medium。
"""
from __future__ import annotations

import pathlib
import textwrap

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "delivery" / "poster.png"

W, H = 1240, 1754
BG = (247, 246, 243)        # 暖白
INK = (32, 36, 43)          # 主文字
INK2 = (91, 96, 104)        # 次要文字
INK3 = (138, 154, 146)      # 弱化
GREEN = (52, 70, 63)        # 深绿：强调与底栏
BAND = (236, 234, 228)      # 条带底
RULE = (217, 214, 207)      # 分隔线
WHITE = (255, 255, 255)

SONG = "/System/Library/Fonts/Supplemental/Songti.ttc"
HEI = "/System/Library/Fonts/STHeiti Medium.ttc"


def font(path, size, index=1):
    return ImageFont.truetype(path, size, index=index)


def wrap(text, fnt, max_w, draw):
    """按像素宽度折行，中英混排不会把词切断。"""
    out, line = [], ""
    for ch in text:
        if ch == "\n":
            out.append(line)
            line = ""
            continue
        trial = line + ch
        if draw.textlength(trial, font=fnt) <= max_w:
            line = trial
        else:
            out.append(line)
            line = ch
    if line:
        out.append(line)
    return out


def para(draw, x, y, text, fnt, max_w, fill=INK, leading=1.62, gap=10):
    for i, ln in enumerate(wrap(text, fnt, max_w, draw)):
        draw.text((x, y), ln, font=fnt, fill=fill)
        y += int(fnt.size * leading)
    return y + gap


def bullets(draw, x, y, items, fnt, max_w, dot=GREEN, leading=1.6, gap=14):
    for it in items:
        draw.ellipse((x, y + int(fnt.size * 0.42), x + 7, y + int(fnt.size * 0.42) + 7),
                     fill=dot)
        yy = y
        for ln in wrap(it, fnt, max_w - 22, draw):
            draw.text((x + 22, yy), ln, font=fnt, fill=INK)
            yy += int(fnt.size * leading)
        y = yy + gap
    return y


def band(draw, x, y, w, h, fill=BAND, radius=10):
    draw.rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=fill)


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_h1 = font(SONG, 96, 1)        # 大标题
    f_sub = font(SONG, 33, 1)       # 副标题
    f_lead = font(HEI, 27)          # 导语
    f_h2 = font(SONG, 36, 1)        # 小节标题
    f_body = font(HEI, 26)          # 正文
    f_small = font(HEI, 22)         # 注释
    f_foot = font(SONG, 26, 1)      # 底栏

    M = 78                          # 左右边距
    CW = W - M * 2                  # 内容宽

    # ── 标题 ──
    y = 96
    d.text((M, y), "我的小助手", font=f_h1, fill=INK)
    y += int(f_h1.size * 1.28)
    sub = "个人 AI 能力底座：跨时间跨应用的记忆，与人办事时记住关键信息、给一版建议"
    for ln in wrap(sub, f_sub, CW, d):
        d.text((M, y), ln, font=f_sub, fill=GREEN)
        y += int(f_sub.size * 1.42)
    y += int(f_sub.size * 0.2)
    d.line((M, y, W - M, y), fill=RULE, width=2)
    y += 34

    # ── 导语 ──
    y = para(d, M, y,
             "知道一个人的情况，不等于可以把他的信息说出去。资料、授权、方案版本、"
             "回应、执行状态分别记录；模型管读图、抽信息、提方案，身份、授权、"
             "预算和版本一致性由程序校验。",
             f_lead, CW, INK2)

    # ── 要解决的问题 ──
    y += 12
    bh = 300
    band(d, M, y, CW, bh)
    d.text((M + 26, y + 22), "要解决的问题", font=f_h2, fill=GREEN)
    bullets(d, M + 26, y + 84, [
        "孩子这周哪条线练了、哪条线很久没动，散在几个 App 里。",
        "妈妈的血压怎么样、药有没有按时吃，数据在她自己的应用和手表里，问不到。",
        "送人什么尺码、喜欢什么、上回送了什么，记在聊天记录和相册里，用的时候靠翻。",
        "几个人约一件事，各人说的条件散着，改一个条件其他几家又得重新对。",
    ], f_body, CW - 52, gap=10)

    y += bh + 30

    # ── 一个助手能做的 ──
    d.text((M, y), "一个助手能做的", font=f_h2, fill=GREEN)
    y += int(f_h2.size * 1.55)
    d.text((M, y), "记住人 · 存资料 · 跟别人办事 · 接自己的应用 · 在电脑上代填",
           font=f_body, fill=INK)
    y += int(f_body.size * 2.2)

    # ── 三个例子 ──
    d.text((M, y), "三个例子", font=f_h2, fill=GREEN)
    y += int(f_h2.size * 1.55)

    examples = [
        ("记孩子的学习进度",
         "按数学、语文、英语、阅读、练琴这些线归组，一条线很久没记会直接说。"
         "线上完成的部分接了应用能自动来；线下的期中期末成绩、课程表拍张图或手输也能录。"),
        ("接妈妈的用药提醒和手表",
         "用药提醒应用、智能手表接进来，问血压怎么样、药有没有按时吃，"
         "答的是这些应用和设备里的数据，她本人不用装你的助理。"),
        ("送礼前先查档案",
         "尺码、偏好、上次送过什么、明确说过不要什么，分开记在自己名下。"
         "说一句「给爸爸买双走路舒服的鞋」，它拿这些去比选，缺的照实问、不编。"),
    ]
    for title, body in examples:
        d.text((M, y), "· " + title, font=font(HEI, 28), fill=INK)
        y += int(28 * 1.7)
        y = para(d, M + 20, y, body, f_body, CW - 20, INK2, gap=16)

    # ── 形态 ──
    y += 6
    bh = 190
    band(d, M, y, CW, bh, fill=(238, 241, 238))
    d.text((M + 26, y + 22), "形态", font=f_h2, fill=GREEN)
    para(d, M + 26, y + 80,
         "手机 Web 只是演示载体，不是最终形态。同一个底座还能长成系统级的个人助理，"
         "或者可被第三方应用与 Agent 调用的能力。",
         f_body, CW - 52, INK2)

    y += bh + 34

    # ── 边界 ──
    y = para(d, M, y,
             "还未解决的：安卓真机未验；完整模型测试套件未复跑；智能手表这类设备仍是预留位；"
             "买卖支付均为模拟。",
             f_small, CW, INK3)

    # ── 底栏 ──
    d.rectangle((0, H - 96, W, H), fill=GREEN)
    d.text((M, H - 72), "github.com/larisfavour-lang/personal-ai-hackathon",
           font=f_foot, fill=WHITE)
    label = "MIT · 2026-10-19"
    d.text((W - M - d.textlength(label, font=f_foot), H - 72), label,
           font=f_foot, fill=(200, 210, 203))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print("已生成", OUT, OUT.stat().st_size, "字节")


if __name__ == "__main__":
    main()
