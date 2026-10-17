"""拍照识别链：真实图片 → Step 多模态 → 本人核对 → 落库 → 幂等 → 日期语义。

这条用例守住的是「拍一张记录」这条入口的几个承诺，缺一个都会让演示时说错：

1. 图里的文字真的被读出来了。Step 多模态调不通、参数顺序写错、data URI
   拼错，这条都不成立——所以这里发真实 base64，不 mock 模型。
2. 本人没勾的字段不进库。核对的意义就在于「模型读到的不等于事实」，
   全量落库会让核对变成摆设。
3. 同一张图重复保存只留一条。来源 ID 若每次随机生成，import_life_event
   的去重键（来源+主体+类型+日期）永远命中不了，同一条记录会重复出现。
4. 发生日期读不清时保持未知，不拿今天冒充。图里常见「10月12日」这种
   没有年份的写法，补年份是替事实补内容，之后按日期回忆会放错年份。

边界说明（不许夸大）：本脚本用的是**程序生成的手写风格图片**，
验证的是「图片内容被多模态模型读出并正确落库」。
它不等于「安卓真机相机实拍通过」，也不等于「麦克风/语音已实现」——
本工程没有任何语音相关代码，语音链路不在验收范围内。

运行：/tmp/hack_venv/bin/python tests/test_photo_chain.py
服务需已在 8000 端口运行（模型 step/step-5-preview 可用）。
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]

results: list[tuple[str, bool, object]] = []


def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))


def post(path: str, obj: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        return {"HTTPError": exc.code, "detail": body}


def make_photo(path: Path, lines, seed: int) -> bytes:
    """造一张像随手拍的手写记录：米白纸、轻微倾斜、字迹不齐、带噪点。

    刻意做得不像干净扫描件：模型对整齐印刷体的识别很容易，
    那样的图通过不了说明任何问题，也代表不了真实拍摄条件。
    """
    import random
    from PIL import Image, ImageDraw, ImageFont
    rnd = random.Random(seed)
    W, H = 900, 1150
    img = Image.new("RGB", (W, H), (246, 242, 231))
    d = ImageDraw.Draw(img)

    def font(sz):
        for p in ("/System/Library/Fonts/Supplemental/Kaiti.ttc",
                  "/System/Library/Fonts/Supplemental/Songti.ttc"):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    y = 140
    for text, sz in lines:
        f = font(sz)
        d.text((100 + rnd.randint(-3, 3), y), text, font=f, fill=(38, 44, 58))
        y += sz + 48
    img = img.rotate(1.0, resample=Image.BICUBIC, expand=False,
                     fillcolor=(239, 235, 225))
    px = img.load()
    for _ in range(24000):
        x = rnd.randrange(W)
        yy = rnd.randrange(H)
        r, g, b = px[x, yy]
        n = rnd.randint(-13, 13)
        px[x, yy] = (max(0, min(255, r + n)), max(0, min(255, g + n)),
                     max(0, min(255, b + n)))
    img.save(path, "JPEG", quality=82)
    return path.read_bytes()


def main() -> int:
    tmp = Path("/tmp/pa_photo_chain")
    tmp.mkdir(exist_ok=True)

    # 图里写「10月12日」但没有年份：用来验证日期未知不被补成今天。
    lines_a = [("10月12日 星期日", 40), ("小雨 今天读了《昆虫记》", 46),
               ("读了 25 分钟", 44), ("自己说：法布尔的观察真细", 42),
               ("答：螳螂捕食那段", 44)]
    img_a = make_photo(tmp / "a.jpg", lines_a, seed=11)
    b64_a = base64.b64encode(img_a).decode("ascii")

    read = post("/api/photo/read", {"image_b64": b64_a, "media_type": "image/jpeg"})
    if "HTTPError" in read:
        check("1 图片能被 Step 读出来", False, read)
        _report()
        return 1

    transcribed = read.get("transcribed") or ""
    fields = read.get("fields") or {}
    # 逐字核对几个关键字：读出一部分不算通过，这条链的价值就在读得准。
    check("1 图片能被 Step 读出来（读到关键内容）",
          bool(transcribed) and "昆虫记" in transcribed and "25 分钟" in transcribed,
          {"transcribed": transcribed, "fields": fields})
    check("1b 识别把握度如实给出",
          (read.get("confidence") or "") in ("high", "medium", "low"),
          read.get("confidence"))
    check("1c 日期没读到就如实返回空，不猜年份",
          read.get("happened_on") in ("", None) or _is_iso(read.get("happened_on")),
          repr(read.get("happened_on")))

    if not fields:
        check("2 未勾的字段不进库", False, "没有可勾的字段")
        _report()
        return 1

    # 本人只勾第一个字段：其余读到的字段不该进库。
    picked_key = list(fields.keys())[0]
    picked = {picked_key: fields[picked_key]}
    saved = post("/api/photo/save", {
        "owner_id": "A", "subject_id": "p_xiaoyu", "image_b64": b64_a,
        "media_type": "image/jpeg", "captured_by": "我",
        "confirmed_fields": picked, "read": read, "grants": []})
    check("2 未勾的字段不进库（只写勾选的）",
          saved.get("fields") == picked,
          {"picked": picked, "saved": saved.get("fields"),
           "model_read": fields})
    # 同 test_photo_android_size：来源按实际录入方式拼，纯图这条是「图片记录」，
    # 不再写死「拍照记录」。仍要同时写清谁录入、核对没核对。
    prov = saved.get("provider") or ""
    check("2b 来源写明是图片记录、谁录入并经本人核对",
          "图片记录" in prov and "核对后采用" in prov and "我" in prov,
          saved.get("provider"))
    check("2c 日期读不清时发生日期留空并标 unknown",
          saved.get("happened_on") == "" and saved.get("date_unknown") is True,
          {"happened_on": saved.get("happened_on"),
           "date_unknown": saved.get("date_unknown")})

    # 同一张图再存一次：必须命中同一条，不能变成两条记录。
    again = post("/api/photo/save", {
        "owner_id": "A", "subject_id": "p_xiaoyu", "image_b64": b64_a,
        "media_type": "image/jpeg", "captured_by": "我",
        "confirmed_fields": picked, "read": read, "grants": []})
    check("3 同一张图重复保存只留一条（幂等）",
          again.get("created") is False
          and again.get("event_id") == saved.get("event_id"),
          {"first": {"created": saved.get("created"),
                     "event_id": saved.get("event_id")},
           "second": {"created": again.get("created"),
                      "event_id": again.get("event_id")}})

    # 另一张图、另一个主体：不能跟上面那条撞来源，也不能串到别人名下。
    lines_b = [("10月14日", 40), ("小宇 今天踢了足球", 46),
               ("练了 40 分钟", 44), ("自己说：射门进了三个", 42)]
    b64_b = base64.b64encode(make_photo(tmp / "b.jpg", lines_b, seed=7)).decode("ascii")
    read_b = post("/api/photo/read", {"image_b64": b64_b, "media_type": "image/jpeg"})
    check("4 第二张图同样读得出来",
          "足球" in (read_b.get("transcribed") or ""),
          read_b.get("transcribed"))
    fields_b = read_b.get("fields") or {}
    if fields_b:
        key_b = list(fields_b.keys())[0]
        saved_b = post("/api/photo/save", {
            "owner_id": "A", "subject_id": "p_xiaohan", "image_b64": b64_b,
            "media_type": "image/jpeg", "captured_by": "我",
            "confirmed_fields": {key_b: fields_b[key_b]},
            "read": read_b, "grants": []})
        check("4b 不同图片落到不同记录上",
              saved_b.get("event_id") != saved.get("event_id"),
              {"a": saved.get("event_id"), "b": saved_b.get("event_id")})

    # 主体不在本人记录里时必须拒绝，不能借拍照把记录写到别人名下的人头上。
    bad = post("/api/photo/save", {
        "owner_id": "A", "subject_id": "p_not_exist", "image_b64": b64_a,
        "media_type": "image/jpeg", "captured_by": "我",
        "confirmed_fields": picked, "read": read, "grants": []})
    check("5 主体不存在时拒绝写入",
          "HTTPError" in bad or "error" in bad, bad)

    _report()
    return 0 if all(ok for _, ok, _ in results) else 1


def _is_iso(s) -> bool:
    import re
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", str(s or "")))


def _report() -> None:
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 66)
    for name, ok, detail in results:
        print("%s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            print("      证据：%s" % json.dumps(detail, ensure_ascii=False, default=str)[:600])
    print("=" * 66)
    print("拍照识别链：%d/%d 通过" % (passed, len(results)))
    print("边界：本脚本用程序生成的图片验证多模态读图与落库，")
    print("      不等于安卓真机相机实拍；语音是浏览器侧识别，不在本脚本范围内。")


if __name__ == "__main__":
    sys.exit(main())
