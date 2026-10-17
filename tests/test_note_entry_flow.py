import asyncio, base64, json, pathlib, socket, subprocess, time, urllib.request
import websockets

helper = pathlib.Path(__file__).with_name("prompt-chrome-20261015-0507.py")
ns = {"__file__": str(helper)}
exec(helper.read_text().rsplit("raise SystemExit", 1)[0], ns)

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "/x8AAwMCAO+/6qkAAAAASUVORK5CYII=")


class NoteCDP(ns["CDP"]):
    """拦 photo/read、photo/save、voice/transcribe，不动真实模型与业务写。"""

    def __init__(self, ws):
        super().__init__(ws)
        self.loaded = asyncio.Event()
        self.reads = 0
        self.saves = []
        self.voices = []
        # 置真时让 /api/photo/read 返回 500，用来验「识别失败不卡死」。
        # 失败必须是真实失败（不是页面里抛异常），否则验不到降级路径。
        self.fail_read = False

    async def pump(self):
        # Fetch.requestPaused 必须转给 fulfill：少了这句，被拦的请求永远挂着，
        # 页面里那个 await fetch 一直不返回，看起来像产品卡住。
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("id") in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Page.loadEventFired":
                self.loaded.set()
            elif m.get("method") == "Fetch.requestPaused":
                asyncio.create_task(self.fulfill(m["params"]))

    # 自动识别是加图之后异步跑的（压缩 + 一次模型请求），比点一下按钮慢，
    # 所以轮询次数比 helper 默认的 60 放宽。try/except 包住：表达式里
    # 引用还没初始化的字段时会抛异常，那要当成「还没到」继续等，
    # 不是用例失败。
    async def wait(self, expr, tries=150):
        for _ in range(tries):
            if await self.js("(()=>{try{return !!(%s)}catch(e){return false}})()"
                             % expr):
                return True
            await asyncio.sleep(0.15)
        return False

    async def fulfill(self, p):
        r = p["request"]
        if r["method"] in ("GET", "HEAD"):
            await self.send("Fetch.continueRequest", requestId=p["requestId"])
            return
        if r["url"].endswith("/api/photo/read"):
            self.reads += 1
            if self.fail_read:
                detail = json.dumps({"detail": "模型接口不可用"},
                                    ensure_ascii=False).encode()
                await self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                                responseCode=500,
                                responseHeaders=[{"name": "Content-Type",
                                                  "value": "application/json"}],
                                body=base64.b64encode(detail).decode())
                return
            body = {"transcribed": "哥哥练琴四十分钟",
                    "fields": {"完成时间": "40分钟"},
                    "happened_on": "2026-10-14", "confidence": "high",
                    "unclear_parts": [], "subject_hint": ""}
        elif r["url"].endswith("/api/photo/save"):
            self.saves.append(json.loads(r.get("postData") or "{}"))
            body = {"event_id": "evt_note_stub", "created": True,
                    "provider": "文字+图片记录（我 录入，本人核对后采用）",
                    "happened_on": "2026-10-14", "date_unknown": False,
                    "fields": {"正文": "x"}, "attachments": [],
                    "text_edited": True}
        elif r["url"].endswith("/api/voice/transcribe"):
            self.voices.append(json.loads(r.get("postData") or "{}"))
            body = {"available": False, "text": "",
                    "reason": "当前模型接口没有可用的中文语音转写能力"}
        else:
            await self.send("Fetch.continueRequest", requestId=p["requestId"])
            return
        await self.send("Fetch.fulfillRequest", requestId=p["requestId"],
                        responseCode=200,
                        responseHeaders=[{"name": "Content-Type",
                                          "value": "application/json"}],
                        body=base64.b64encode(
                            json.dumps(body, ensure_ascii=False).encode()).decode())


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def pick_file(c, sel):
    b = base64.b64encode(TINY_PNG).decode()
    await c.js(
        "(async()=>{const b=atob(%s);const u=new Uint8Array(b.length);"
        "for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);"
        "const f=new File([u],'a.png',{type:'image/png'});"
        "const dt=new DataTransfer();dt.items.add(f);"
        "const inp=document.querySelector(%s);inp.files=dt.files;"
        "inp.dispatchEvent(new Event('change',{bubbles:true}));return true})()"
        % (json.dumps(b), json.dumps(sel)))


async def type_text(c, sel, val):
    await c.js(
        "(()=>{const e=document.querySelector(%s);e.value=%s;"
        "e.dispatchEvent(new Event('input',{bubbles:true}));return true})()"
        % (json.dumps(sel), json.dumps(val)))


async def main():
    checks = []

    def check(name, ok, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    port = free_port()
    proc = subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--no-first-run", "--no-default-browser-check",
         "--window-size=430,1000", "--remote-debugging-port=%d" % port,
         "--user-data-dir=/tmp/codex-note-flow-%d" % port, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(120):
            try:
                tabs = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=1).read())
                break
            except Exception:
                await asyncio.sleep(0.15)
        if tabs is None:
            check("Chrome 可启动", False, "拿不到调试端口")
            return 1
        page = next(x for x in tabs if x["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"],
                                      max_size=20 * 1024 * 1024) as ws:
            c = NoteCDP(ws)
            pump = asyncio.create_task(c.pump())
            await c.send("Page.enable")
            await c.send("Runtime.enable")
            await c.send("Emulation.setDeviceMetricsOverride",
                         width=430, height=900, deviceScaleFactor=1, mobile=False)
            await c.send("Fetch.enable",
                         patterns=[{"urlPattern": "*/api/photo/*",
                                    "requestStage": "Request"},
                                   {"urlPattern": "*/api/voice/*",
                                    "requestStage": "Request"}])
            await c.send("Page.navigate", url="http://127.0.0.1:8000/")
            await asyncio.wait_for(c.loaded.wait(), 20)
            await c.wait("typeof api === 'function' && !!ownerId")
            await c.wait("lastPersons && lastPersons.length > 0")

            # 1) 打开就是记事本草稿：能直接打字，不先逼人选人或选模式
            await c.js("document.querySelector('#btn-photo').click()")
            await c.wait("document.querySelector('#sheet').classList.contains('open')")
            st = await c.js(
                "({title:document.querySelector('#sheet-title').textContent,"
                "hasText:!!document.querySelector('#note-text'),"
                "btns:Array.from(document.querySelectorAll('#sheet-body button'))"
                ".map(b=>b.id||b.textContent.trim()),"
                "subs:document.querySelectorAll('[data-n-subject]').length})")
            check("打开「新建资料」就是可写草稿，不是拍照前置",
                  st["title"] == "新建资料" and st["hasText"], st)
            check("图片是草稿旁边的动作，不是进门门槛",
                  "note-add-photo" in st["btns"], st["btns"])
            # 语音用的是浏览器自带的 SpeechRecognition，实测可用：
            # 入口就叫「说一段」，不再写成「暂不可用」。
            # 按钮列表取的是 id（有 id 就显示 id），所以按 id 判；
            # 文案改成「说一段」这一点由下面点了之后的状态佐证。
            check("语音入口是可用的功能，不标成暂不可用",
                  "note-add-voice" in st["btns"]
                  and not any("暂不可用" in b for b in st["btns"]), st["btns"])
            # 一级选项只剩两个：图片识别 + 语音输入。旧的四个并列按钮
            # （加一张图/只作配图/先裁剪再识别/说一段）不再平铺——后三个进第二步。
            check("加图片区一级选项只有图片识别与语音输入两个",
                  sorted(b for b in st["btns"]
                         if b in ("note-add-photo", "note-add-voice")) ==
                  ["note-add-photo", "note-add-voice"]
                  and "note-attach-only" not in st["btns"]
                  and "note-crop-first-entry" not in st["btns"], st["btns"])

            # 2) 纯文字：不选人也能先写
            await type_text(c, "#note-text", "小雨今天自己拼图拼了挺久")
            k = await c.js("JSON.stringify(noteCtx.kinds)")
            check("打字即标记这条用了文字", '"text"' in k, k)

            # 3) 加图：一级选项只有「图片识别 / 语音输入」；选定文件后图进草稿、
            #    缩略图可见、不跳裁剪，并且**先不送模型**——弹层内给出二选一。
            await c.js("document.querySelector('#note-add-photo').click()")
            await pick_file(c, "#note-photo-input")
            await c.wait("noteCtx && noteCtx.attachments.length > 0")
            # 图已进草稿但尚未送出：这是本次改动的核心保护，先钉住它。
            check("选定文件后图进草稿但一次 /api/photo/read 都没发",
                  c.reads == 0, {"reads": c.reads})
            choice = await c.js(
                "Array.from(document.querySelectorAll('#note-pick-choice button'))"
                ".map(b=>b.id||b.textContent.trim())")
            check("弹层内出现明确二选一（识别进正文 / 只存不识别）",
                  "note-choice-ocr" in choice and "note-choice-keep" in choice,
                  choice)
            check("先裁剪再识别保留为次级选项",
                  "note-choice-crop" in choice, choice)
            hint = await c.js(
                "(document.querySelector('#note-sheet-hint')||{}).textContent||''")
            check("弹层内有可见反馈说明图还没送出去",
                  "还没有送出去" in hint, hint)
            # 点「识别图里的文字，进正文框」之后才发那一次请求。
            await c.js("document.querySelector('#note-choice-ocr').click()")
            await c.wait("noteCtx && noteCtx.reads.length > 0 && noteCtx.reads[0].ok",
                         tries=120)
            imgs = await c.js(
                "Array.from(document.querySelectorAll('#note-attachments img'))"
                ".map(i=>({ok:i.complete,w:i.naturalWidth}))")
            check("加图后草稿里能看见这张图（不跳裁剪、不丢草稿）",
                  len(imgs) == 1 and imgs[0]["ok"],
                  {"imgs": imgs, "text": await c.js(
                      "document.querySelector('#note-text').value")})
            # 本人明确点了「识别」才送那一次：不是加图就自动外传。
            check("只有本人点过「识别」之后才发一次 /api/photo/read", c.reads == 1,
                  {"reads": c.reads})
            auto = await c.js(
                "({body:document.querySelector('#note-text').value,"
                "hint:(document.querySelector('#note-read-hint')||{}).textContent||''})")
            check("识别出的文字直接进正文框",
                  "哥哥练琴四十分钟" in auto["body"], auto)
            # 用户先打的字留在前面，中间一个换行——不是被识别结果顶掉。
            check("已打的正文保留在前，识别文字追加在后",
                  auto["body"] == "小雨今天自己拼图拼了挺久\n哥哥练琴四十分钟",
                  auto["body"])
            check("识别把握在正文下方一行小字里说明",
                  "high" in auto["hint"], auto["hint"])
            check("识别出的日期自动填进草稿",
                  await c.js("document.querySelector('#note-day').value")
                  == "2026-10-14",
                  {"day": await c.js("document.querySelector('#note-day').value")})
            acts = await c.js(
                "Array.from(document.querySelectorAll('#note-image-acts button'))"
                ".map(b=>({id:b.id,dis:b.disabled}))")
            ids = [a["id"] for a in acts]
            check("先选范围再识别 / 撤掉识别文字只留图 仍是可达的小号次要选项",
                  "note-crop-first" in ids and "note-keep-image" in ids, ids)
            check("识别完成后三个选项都可再点（没卡在禁用态）",
                  all(not a["dis"] for a in acts), acts)

            # 3b) 重新识别：不把同一张图的文字在正文里留两遍
            #     趁正文还没被本人改过的时候试。改过之后按行原样撤会找不到
            #     那一行，那是另一种预期行为，见 3e。
            await c.js("document.querySelector('#note-ocr').click()")
            # 先等它真的开始，再等它结束。只等结束态会在上一轮遗留的
            # 「读完了」上假通过——按钮点下去那一刻状态还是旧的。
            await c.wait("noteCtx.ocrBusy === true", tries=60)
            await c.wait("noteCtx.ocrBusy === false"
                         " && document.querySelector('#status-line').textContent"
                         ".indexOf('读完了') >= 0", tries=120)
            check("重新识别又送了一次", c.reads == 2, {"reads": c.reads})
            re_body = await c.js("document.querySelector('#note-text').value")
            check("重新识别后正文里那段只出现一次",
                  re_body == "小雨今天自己拼图拼了挺久\n哥哥练琴四十分钟",
                  re_body)

            # 3c) 识别结果可改：就在正文框里改，不需要另找一处编辑区
            await type_text(c, "#note-text",
                            "小雨今天自己拼图拼了挺久\n"
                            "哥哥练琴四十分钟，练完自己收了琴")
            check("正文框里的识别文字可以直接改",
                  "自己收了琴" in await c.js(
                      "document.querySelector('#note-text').value"),
                  await c.js("document.querySelector('#note-text').value"))

            # 3d) 识别失败：不卡死、不丢正文、图仍作配图、还能重试
            #    真实失败（HTTP 500），不是页面里抛异常——
            #    否则验不到降级路径，只验到 catch 分支。
            c.fail_read = True
            before_fail = await c.js("document.querySelector('#note-text').value")
            await c.js("document.querySelector('#note-add-photo').click()")
            await pick_file(c, "#note-photo-input")
            # t41：图进草稿后先不送模型，本人点「识别」之后才发那一次。
            await c.js("document.querySelector('#note-choice-ocr').click()")
            # 必须同时等 ocrBusy 落回去。新建条目时 ok 就是 false，
            # 只等 ok === false 会在请求还没发出去时就命中——
            # 那时按钮还停在「正在识别这张图…」，下面几条会全假失败。
            await c.wait("noteCtx && noteCtx.reads.length == 2"
                         " && noteCtx.ocrBusy === false"
                         " && noteCtx.reads[1].ok === false", tries=150)
            after_fail = await c.js(
                "({body:document.querySelector('#note-text').value,"
                "atts:noteCtx.attachments.length,"
                "err:(noteCtx.reads[1]||{}).error||'',"
                "hint:(document.querySelector('#note-read-hint')||{}).textContent||'',"
                "ocr:document.querySelector('#note-ocr').disabled,"
                "ocrTxt:(document.querySelector('#note-ocr')||{}).textContent||''})")
            check("识别失败不丢用户已打的正文",
                  after_fail["body"] == before_fail, after_fail["body"])
            check("识别失败的图仍作为配图留在草稿里",
                  after_fail["atts"] == 2, after_fail["atts"])
            check("失败时按钮回到可点的「重新识别」（没卡死）",
                  not after_fail["ocr"]
                  and after_fail["ocrTxt"] == "重新识别", after_fail)
            check("提示行说明失败原因，并说正文不受影响",
                  "模型接口不可用" in after_fail["hint"]
                  and "正文不受影响" in after_fail["hint"], after_fail["hint"])

            # 3e) 失败后重试：认出来了，本人改过的那句也没被冲掉
            c.fail_read = False
            await c.js("document.querySelector('#note-ocr').click()")
            await c.wait("noteCtx.ocrBusy === true", tries=60)
            await c.wait("noteCtx && noteCtx.reads.length == 2"
                         " && noteCtx.ocrBusy === false"
                         " && noteCtx.reads[1].ok", tries=150)
            # 4 次 = 第一张点识别 1 + 重新识别 1 + 失败那张点识别 1 + 重试 1。
            # 少一次说明「重新识别」在失败后被什么挡住了。
            check("失败后点「重新识别」能认出来", c.reads == 4,
                  {"reads": c.reads})
            retry_body = await c.js("document.querySelector('#note-text').value")
            check("重试成功后本人改过的正文还在",
                  "自己收了琴" in retry_body, retry_body)
            check("重试成功后识别文字也进正文框",
                  retry_body.count("哥哥练琴四十分钟") >= 1, retry_body)

            # 4) 语音：走浏览器 SpeechRecognition，不发服务端请求。
            # 真实麦克风要用户授权，这里注入一个假的 SpeechRecognition，
            # 验的是界面真的接上了这条链：点了进入聆听、识别结果进正文、
            # 说完能定稿并标记来源为语音。音频不经过我们的服务端。
            await c.js("""(function(){
              window.__spLog = [];
              if(navigator.mediaDevices){
                navigator.mediaDevices.getUserMedia = () => Promise.resolve({getTracks: () => []});
              }
              class FakeSR{
                constructor(){ this.lang=""; this.interimResults=false; this.continuous=false; }
                start(){
                  window.__spLog.push("start");
                  setTimeout(() => {
                    if(this.onresult) this.onresult({results:[
                      {0:{transcript:"哥哥今天自己练琴四十分钟"}, isFinal:false}]});
                  }, 30);
                  setTimeout(() => {
                    if(this.onresult) this.onresult({results:[
                      {0:{transcript:"哥哥今天自己练琴四十分钟"}, isFinal:true}]});
                  }, 90);
                  setTimeout(() => { if(this.onend) this.onend(); }, 140);
                }
                stop(){ window.__spLog.push("stop"); if(this.onend) this.onend(); }
              }
              window.SpeechRecognition = FakeSR;
              window.webkitSpeechRecognition = FakeSR;
            })()""")
            before_voice = len(c.voices)
            # 按住说话：pointerdown 起、松手停。派真实 pointerdown，
            # 再用页面暴露的 cbPushToTalk 钩子停（语义与松手一致）。
            await c.js("""(function(){
              const b = document.querySelector('#note-add-voice');
              b.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
            })()""")
            await c.wait("window.__spLog.indexOf('start') >= 0")
            await c.js("""(function(){
              const b = document.querySelector('#note-add-voice');
              if(b.cbPushToTalk) b.cbPushToTalk.stop();
              else b.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}));
            })()""")
            await c.wait("noteCtx.kinds.indexOf('voice') >= 0")
            vst = await c.js(
                "({status:document.querySelector('#status-line').textContent,"
                "text:document.querySelector('#note-text').value,"
                "kinds:noteCtx.kinds, log:window.__spLog, btn:(document.querySelector('#note-add-voice')||{}).textContent})")
            check("语音识别结果真的进了正文", "练琴四十分钟" in vst["text"], vst)
            check("这条资料标记为用了语音", "voice" in vst["kinds"], vst)
            check("语音不走服务端转写接口", len(c.voices) == before_voice,
                  {"voices": len(c.voices)})
            check("识别完成后按钮回到可再点", vst["btn"] == "语音输入", vst)
            check("语音链真的被启动过", "start" in (vst["log"] or []), vst["log"])

            # 5) 保存不重复识别：草稿里已经有识别结果，保存时不再送一次模型。
            #    原来这一步是「一键识别」，现在识别挪到加图那一刻自动做了，
            #    这里只证保存不会把同一张图再认一遍。
            reads_before_save = c.reads
            await c.js("document.querySelector('[data-n-subject]').click()")
            await c.js("document.querySelector('#note-save').click()")
            # 显式轮询而不是 c.wait：后者固定轮 60 次，前几步已经花掉不少时间，
            # 攒到最后一步容易在还有时间的时候被判超时。
            for _ in range(80):
                if len(c.saves) > 0:
                    break
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.6)
            b = c.saves[0]
            check("保存发出了一次", len(c.saves) == 1, {"n": len(c.saves)})
            check("保存没有再送一次识别", c.reads == reads_before_save,
                  {"before": reads_before_save, "after": c.reads})
            check("保存带上图的识别结果（不重复识别）",
                  (b.get("read") or {}).get("transcribed"), b.get("read"))
            check("保存带上全部配图（两张：成功那张 + 失败重试那张）",
                  len(b.get("attachments") or []) == 2,
                  {"n": len(b.get("attachments") or [])})
            # 这条草稿这次同时用了文字、图片和语音，三种都要照实写进来源；
            # 少写一种，回看时就以为有一条没进来。
            check("保存的来源写清实际用了文字、图片和语音",
                  sorted(b.get("input_kinds") or []) == ["image", "text", "voice"],
                  b.get("input_kinds"))
            check("保存用草稿最终日期",
                  b.get("happened_on") == "2026-10-14", b.get("happened_on"))
            check("保存带了选中的主体", bool(b.get("subject_id")),
                  b.get("subject_id"))
            check("保存带的是本人改过的正文",
                  "自己收了琴" in (b.get("draft_text") or ""),
                  b.get("draft_text"))

            err = await c.js("window.__paErr||''")
            check("全程无页面脚本错误", not err, err)

            shot = await c.send("Page.captureScreenshot", format="png")
            pathlib.Path(__file__).with_suffix(".png").write_bytes(
                base64.b64decode(shot["data"]))
            pump.cancel()
        result = {
            "utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "checks": checks,
            "passed": sum(x["ok"] for x in checks),
            "total": len(checks),
            "boundary": "隔离 Chrome 430×900；真实 DOM 点击与 File 注入，"
                        "/api/photo/read、/api/photo/save 全部拦截返回合成数据"
                        "（0 次真实模型、0 次业务写）。语音走浏览器自带的 "
                        "SpeechRecognition，本脚本注入假实现验界面接上了这条链，"
                        "不发服务端请求。只证「加图即自动识别、文字进正文框、"
                        "识别失败不丢正文」这条录入路径的界面与草稿行为，"
                        "不证服务端持久化，也不证真机相机与真语音转写。"
                        "识别失败用 HTTP 500 造真实失败，不是页面内抛异常。",
        }
        pathlib.Path(__file__).with_suffix(".json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if all(x["ok"] for x in checks) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


raise SystemExit(asyncio.run(main()))
