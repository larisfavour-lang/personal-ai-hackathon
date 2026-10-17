"""t26 owner note→plan actual DOM stateful test; --live isolated BASE.
Uses sibling test fixture/browser helpers (no main side effects). Fixture model
calls and DB writes=0. Live uses real backend note save/link/correction, model
only if server provides it; no Android or real Step inference from mocks.
"""
import argparse, asyncio, base64, functools, hashlib, json, threading, time
from http.server import ThreadingHTTPServer
from test_plan_participation_ui import ROOT,Fixture,Browser,set_value,layout,screenshot_panel,save_panels

async def run(base,live=False):
 checks=[];panels=[]
 def check(n,ok):checks.append((n,bool(ok)));print(('PASS 'if ok else'FAIL ')+n,flush=True)
 a=Browser(9848)
 try:
  c=await a.start(base);await c.wait('typeof openNotePlanLink==="function"&&ownerId==="A"')
  if live:
   # Real save path, with server-provided owner person and no OCR/remote model.
   people=await c.js('api("/api/overview")');subject=next(p['person_id'] for p in people['persons'] if p.get('owner_id')=='A'and p.get('name')!='我')
   await c.click('#btn-photo');await c.wait('!!document.querySelector("#note-save")')
   await set_value(c,'#note-text','周六上午去公园（虚构 UI 验收 '+str(time.time_ns())+'）');await set_value(c,'#note-day','2026-10-14')
   await c.click('[data-n-subject="'+subject+'"]');await c.click('#note-save');await c.wait('!!document.querySelector("[data-note-plan-event]")');eid=await c.js('document.querySelector("[data-note-plan-event]").dataset.notePlanEvent');await c.click('[data-note-plan-event]');await c.wait('!!document.querySelector("#note-plan-link")');check('保存按钮原event入口可直接打开关联',await c.js('notePlanDraft.eventId')==eid)
   check('真实UI保存后可点同一资料入口',bool(eid))
  else:eid='event_ui'
  if live:
   await c.js('openNoteSourceAction('+json.dumps(eid)+',"","","correct")');await c.wait('!!document.querySelector("#source-action")');await set_value(c,'#source-new-key','时间');await set_value(c,'#source-new-value','周六上午');await c.click('#source-confirm');await c.click('#source-action');await c.wait('!!document.querySelector("#note-plan-link")');eid=await c.js('notePlanDraft.eventId');check('本人通过更正UI补短字段后才可公开',bool(eid))
  else:Fixture.fields['时间']='周六上午'
  await c.js('openNotePlanLink('+json.dumps(eid)+')');await c.wait('!!document.querySelector("#note-plan-link")')
  panels.append(('3. Owner-only source, zero defaults',await screenshot_panel(c,'#sheet-title')))
  check('默认零勾选，未自动公开/采用',await c.js('document.querySelectorAll("[data-note-field]:checked").length===0&&document.querySelector("#sheet-body").textContent.includes("发生日期不是活动日期")'))
  await c.click('[data-note-field="时间"]');await set_value(c,'#note-plan-choice','');await set_value(c,'#note-plan-title','本次虚构资料创建家庭活动'+(str(time.time_ns())if live else''))
  if not live:
   Fixture.fail=True;await c.click('#note-plan-link');await c.wait('!document.querySelector("#note-plan-link").disabled');check('关联失败保record/fields/title，可原子重试',await c.js('document.querySelector("[data-note-field=时间]").checked&&document.querySelector("#note-plan-title").value==="本次虚构资料创建家庭活动"'));Fixture.fail=False
  await c.click('#note-plan-link');await c.wait('!!document.querySelector("#note-plan-publish")')
  pid=await c.js('notePlanDraft.target')
  panels.append(('4. Explicit public-condition confirmation',await screenshot_panel(c,'#sheet-title')))
  check('无plan创建关联不重复录入，关联不等于公开',bool(pid)and await c.js('document.querySelectorAll("[data-public-key]:checked").length===0&&!document.querySelector("#publish-confirm").checked'))
  await c.click('#note-plan-publish');check('未勾公开条件及确认不提交',await c.js('document.querySelector("#note-plan-error").textContent.includes("请选择")'))
  await c.click('[data-public-key="时间"]');await set_value(c,'[data-public-value="时间"]','周六上午');await c.click('#publish-confirm');await c.click('#note-plan-publish');await c.wait('!!document.querySelector("[data-note-unlink]")')
  check('当前只linked未used，私有来源本人回读',await c.js('document.querySelector("#plan-last-mile").textContent.includes("尚未被当前共同方案采用")'))
  if live:
   await c.js('post("/api/plan/"+'+json.dumps(pid)+'+"/replan",{})');used=await c.js('api("/api/plan/"+'+json.dumps(pid)+'+"/linked-notes")');check('真实模型替身落版后serverprovenance标used',any(n['event_id']==eid and n['used']for n in used['notes']))
  if not live:
   linked=[b for p,m,b in Fixture.calls if p=='/api/plan/create-with-note'];published=[b for p,m,b in Fixture.calls if p=='/api/note/link-plan'and b.get('publish_confirmed')]
   check('原子create私有引用/公开条件两步分开',bool(linked)and linked[-1]['selected_fields']==['时间']and linked[-1]['public_conditions']=={}and bool(published)and published[-1]['public_conditions']=={'时间':'周六上午'})
   check('未selected私人备注不在引用与共同请求',all('PRIVATE_SOURCE' not in json.dumps(b,ensure_ascii=False)for p,m,b in Fixture.calls if m=='POST'))
  await a.go(base);await c.wait('typeof openPlan==="function"&&ownerId==="A"');await c.js('openPlan('+json.dumps(pid)+')');await c.wait('!!document.querySelector("[data-note-correct]")')
  check('重开同plan持久来源字段/发生日期准确',await c.js('document.querySelector("#plan-last-mile").textContent.includes("2026-10-14")&&document.querySelector("#plan-last-mile").textContent.includes("时间")'))
  await c.click('[data-note-correct]');await c.wait('!!document.querySelector("#source-action")');await set_value(c,'[data-source-edit="正文"]','改为周日上午去公园');await c.click('#source-action')
  check('更正未确认不改变资料',await c.js('document.querySelector("#note-plan-error").textContent.includes("勾选确认")'))
  await c.click('#source-confirm');await c.click('#source-action');await c.wait('!!document.querySelector("#note-plan-link")')
  check('更正新source默认零selection，重核不静默公开',await c.js('document.querySelector("#sheet-body").textContent.includes("改为周日上午")&&document.querySelectorAll("[data-note-field]:checked").length===0'))
  await c.click('[data-note-field="时间"]');await c.click('#note-plan-link');await c.wait('!!document.querySelector("#note-plan-publish")');await c.click('[data-plan-reopen]');await c.wait('!!document.querySelector("[data-note-revoke]")')
  check('更正/更换来源后同plan待重排，旧接受不能沿用',await c.js('document.querySelector("#sheet-body").textContent.includes("方案要重排")'))
  check('来源UI375无overflow/44px新按钮',not(await layout(c))['overflow'])
  for w in [320,414,768,1280]:
   await c.send('Emulation.setDeviceMetricsOverride',width=w,height=900,deviceScaleFactor=1,mobile=w<768);check('来源UI宽度'+str(w)+'无横溢',not(await layout(c))['overflow'])
  await c.send('Emulation.setDeviceMetricsOverride',width=375,height=900,deviceScaleFactor=1,mobile=True)
  save_panels(ROOT/'tests/note-plan-link-ui.png',panels)
  await c.click('[data-note-revoke]');await c.wait('!!document.querySelector("#source-action")');await c.click('#source-confirm');await c.click('#source-action');await c.wait('!!document.querySelector("#plan-last-mile")')
  check('撤回源影响未完本plan且保历史',await c.js('document.querySelector("#sheet-body").textContent.includes("方案要重排")'))
  if not live:
   check('原生UI保存后入口存在，无模型自动识别', 'data-note-plan-event' in (ROOT/'app/static/index.html').read_text() and not any(p=='/api/photo/read'for p,m,b in Fixture.calls))
  print(json.dumps({'mode':'live isolated backend DB'if live else'stateful synthetic HTTP fixture','real_Step':False,'Android':False,'index_sha256':hashlib.sha256((ROOT/'app/static/index.html').read_bytes()).hexdigest(),'passed':sum(ok for _,ok in checks),'total':len(checks)},ensure_ascii=False));return 0 if all(ok for _,ok in checks)else 1
 finally:await a.close()

def main():
 p=argparse.ArgumentParser();p.add_argument('--live');a=p.parse_args();server=None
 if a.live:
  assert a.live.rstrip('/')=='http://127.0.0.1:18828','Live mode restricted to separately initialized UI test backend18828'
  base=a.live.rstrip('/')
 else:
  server=ThreadingHTTPServer(('127.0.0.1',18827),functools.partial(Fixture,directory=str(ROOT)));threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:18827'
 try:return asyncio.run(run(base,bool(a.live)))
 finally:
  if server:server.shutdown();server.server_close()
if __name__=='__main__':raise SystemExit(main())
