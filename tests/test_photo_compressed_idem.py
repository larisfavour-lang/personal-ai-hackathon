"""缩图的幂等：同一张安卓原图存两次只留一条。

source_id 由 image_b64 的 SHA1 决定。前端压缩是确定性的（同样原图 →
同样长边、同样质量 → 同样字节），所以同一张图两次应该得到同一个
source_id。这条不成立，用户手抖点两次「记入」就会出现两条相同记录。

如实说明：这里的缩图由 Pillow 完成，参数与前端一致，但没有执行浏览器
canvas。所以这条验的是「同参数缩图字节相同 → 来源摘要相同 → 只留一条」。
前端 canvas 在不同浏览器/版本上是否产出同样字节，仍需安卓真机复核；
若真机上两次编码字节不同，同一张图会得到两个来源摘要，出现两条记录。
"""
from __future__ import annotations
import base64, io, json, subprocess, sys
from pathlib import Path

BASE = "http://127.0.0.1:8000"
results = []

def check(name, ok, detail=None):
    results.append((name, bool(ok), detail))

def post(path, obj, timeout=300):
    body = Path("/tmp/pa_idem_body.json")
    body.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    p = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-X", "POST", BASE + path,
         "-H", "Content-Type: application/json", "--data-binary", "@" + str(body)],
        capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"HTTPError": "unparseable", "raw": p.stdout[:300]}

def compress(raw, edge=1600, q=85):
    """与前端一致：长边 1600、JPEG 质量 85。"""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    w, h = im.size
    scale = min(1, edge / max(w, h))
    buf = io.BytesIO()
    im.resize((round(w*scale), round(h*scale)), Image.LANCZOS).save(buf, "JPEG", quality=q)
    return buf.getvalue()

def main() -> int:
    src = Path("/tmp/pa_big_android.jpg")
    if not src.exists():
        check("测试原图存在", False, str(src))
        _report(); return 1
    raw = src.read_bytes()

    # 同参数缩图必须是确定性的：两次缩出同样的字节，来源摘要才会相同。
    c1, c2 = compress(raw), compress(raw)
    check("同参数缩图确定性（同图两次同字节）", c1 == c2,
          {"a": len(c1), "b": len(c2)})

    b64 = base64.b64encode(c1).decode("ascii")
    read = post("/api/photo/read", {"image_b64": b64, "media_type": "image/jpeg"})
    fields = read.get("fields") or {}
    if not fields:
        check("压缩图能读出字段", False, read)
        _report(); return 1
    k = list(fields.keys())[0]
    picked = {k: fields[k]}

    body = {"owner_id": "A", "subject_id": "p_xiaohan", "image_b64": b64,
            "media_type": "image/jpeg", "captured_by": "我",
            "confirmed_fields": picked, "read": read, "grants": []}
    first = post("/api/photo/save", dict(body))
    second = post("/api/photo/save", dict(body))
    check("压缩图重复保存只留一条（幂等）",
          second.get("created") is False
          and second.get("event_id") == first.get("event_id"),
          {"first": {"created": first.get("created"), "id": first.get("event_id")},
           "second": {"created": second.get("created"), "id": second.get("event_id")}})

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
    print("压缩图幂等：%d/%d 通过" % (passed, len(results)))
    print("边界：压缩参数与前端一致；真机 canvas 编码字节可能因浏览器版本不同，")
    print("      真机上同图两次的字节一致性需实机复核。")

if __name__ == "__main__":
    sys.exit(main())
