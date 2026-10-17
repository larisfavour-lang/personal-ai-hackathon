"""安卓尺寸原图 → 同参数缩图 → 服务端多模态读图 → 落库。

验的是「相机原图经过缩图后，这条链照样走得通」：把缩图产物真的发给
/api/photo/read，看模型还能不能读出图里的字。这一条不过，前面的缩图就是白做。

如实说明本脚本的位置：这里的缩图由 Pillow 完成，参数（长边 1600、JPEG 85）
与前端 fileToCompressedB64 一致，但**没有执行浏览器 canvas**。所以它验的是
「同参数的图能被模型读出并落库」，不是「前端 canvas 产出的字节能被读出」。
前端 canvas 的字节稳定性仍需安卓真机复核。

网络用 curl 走，不用 python socket（沙箱不允许）。
"""
from __future__ import annotations
import base64, io, json, subprocess, sys
from pathlib import Path

BASE = "http://127.0.0.1:8000"
results = []

def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))

def post(path, obj, timeout=300):
    """把 JSON 写临时文件，用 curl 提交。返回 dict；HTTP 错误也解析成 dict。"""
    body = Path("/tmp/pa_photo_body.json")
    body.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    p = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-X", "POST", BASE + path,
         "-H", "Content-Type: application/json", "--data-binary", "@" + str(body)],
        capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"HTTPError": "unparseable", "raw": p.stdout[:400], "stderr": p.stderr[:200]}

def main() -> int:
    src = Path("/tmp/pa_big_android.jpg")
    if not src.exists():
        check("安卓尺寸原图存在", False, str(src))
        _report()
        return 1
    raw = src.read_bytes()
    check("安卓尺寸原图存在（4000×3000 级）", True, {"bytes": len(raw)})

    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    w, h = im.size
    scale = min(1, 1600 / max(w, h))
    cw, ch = round(w * scale), round(h * scale)
    buf = io.BytesIO()
    im.resize((cw, ch), Image.LANCZOS).save(buf, "JPEG", quality=85)
    comp = buf.getvalue()
    b64 = base64.b64encode(comp).decode("ascii")
    check("压缩后长边为 1600 且比例保持", (cw, ch) == (1600, 1200),
          {"in": (w, h), "out": (cw, ch)})
    check("压缩后体积显著小于原图", len(comp) < len(raw) * 0.5,
          {"raw": len(raw), "compressed": len(comp)})
    # 只陈述本图的实际体积，不把「一定超限」当断言：2.8MB 的图本来就在
    # 6MB 上限之内，压缩是为了让上传更快、更省流量，不是唯一的通过条件。
    check("缩图后体积远小于 6MB 服务端上限",
          len(comp) < 1024 * 1024, {"compressed": len(comp)})

    read = post("/api/photo/read", {"image_b64": b64, "media_type": "image/jpeg"})
    if "HTTPError" in read:
        check("压缩后的图能被 Step 读出来", False, read)
        _report()
        return 1
    transcribed = read.get("transcribed") or ""
    check("压缩后的图能被 Step 读出来（读到关键内容）",
          bool(transcribed) and "足球" in transcribed and "40 分钟" in transcribed,
          {"transcribed": transcribed, "fields": read.get("fields")})
    check("识别把握度如实给出",
          (read.get("confidence") or "") in ("high", "medium", "low"),
          read.get("confidence"))

    fields = read.get("fields") or {}
    if fields:
        k = list(fields.keys())[0]
        saved = post("/api/photo/save", {
            "owner_id": "A", "subject_id": "p_xiaohan", "image_b64": b64,
            "media_type": "image/jpeg", "captured_by": "我",
            "confirmed_fields": {k: fields[k]}, "read": read, "grants": []})
        check("压缩图落库成功且只写勾选字段",
              saved.get("fields") == {k: fields[k]},
              {"saved": saved.get("fields"), "model_read": fields})
        # 来源要写清三件事：用什么记的（这条是图）、谁记的、核对没核对。
        # 断言按当前实现：c6d4471 起来源按实际录入方式拼（图片/文字/语音），
        # 纯文字记录不再显示成「拍照记录」——否则只打了字的用户回看会以为图丢了。
        # 老断言写死「拍照记录」，在这次改动后从没复跑过，两处一起过时。
        prov = saved.get("provider") or ""
        check("来源写明是图片记录、谁录入并经本人核对",
              "图片记录" in prov and "核对后采用" in prov and "我" in prov,
              saved.get("provider"))

    _report()
    return 0 if all(ok for _, ok, _ in results) else 1

def _report():
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 66)
    for name, ok, detail in results:
        print("%s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            print("      证据：%s" % json.dumps(detail, ensure_ascii=False, default=str)[:600])
    print("=" * 66)
    print("安卓原图压缩后识别链：%d/%d 通过" % (passed, len(results)))
    print("边界：原图由程序按 4000×3000 生成，压缩参数与前端一致；")
    print("      真机相机原图的编码体积与对焦效果仍需安卓实机复核。")

if __name__ == "__main__":
    sys.exit(main())
