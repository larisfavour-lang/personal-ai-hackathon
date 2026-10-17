# 用自己的模型接口运行

运行者在电脑上启动服务，普通手机用户打开已启动的网页，不需要配模型或切换他人身份。源码不携带本机代理、模型密钥、运行数据库或 Python 虚拟环境。

## 从零开始

需要 Python 3.10+，本轮观测测试环境为 Python 3.11；依赖是最低版本范围，不是锁版本，也未做另一台电脑的干净安装验收。守护启动使用 POSIX `fork`，下面按 macOS／Linux 说明，不保证 Windows 原样可用。

在解压后的作品根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
PA_MODEL_BASE_URL="https://你的接口地址/v1" \
PA_MODEL_NAME="你的模型名" PA_MODEL_API_KEY="你的密钥" \
python start_server.py 8000
```

OpenAI 兼容接口须支持聊天，图片识别还需支持 `image_url` 多模态。运行者自行确认费用与隐私条款。已有本机代理时可直接执行 `python start_server.py 8000`，默认接口为 `http://127.0.0.1:10100/v1`，默认模型标签为 `step/step-5-preview`；别的电脑没有该代理，不能照抄默认配置就认为模型可用。

启动打印本机和当前局域网地址。电脑用 `http://127.0.0.1:8000`，手机用同网地址。服务默认监听 `0.0.0.0`；仅本机使用时设 `PA_BIND=127.0.0.1`。`PA_LAN_IP` 可指定展示的局域网 IP，不改变监听或认证。正式 Chrome 插件固定站点权限包含本机 8000／8097／9000／8098；应用换端口不代表插件会自动跟随，示例统一使用 8000。

只想前台运行时可用 `python run.py`，默认仅监听 `127.0.0.1`，端口读 `PORT`；终端关闭会结束这个前台进程。它不能直接作为手机局域网入口。

Windows PowerShell 可在作品根目录先建环境，再设自己的模型变量并前台运行；该路径仅按入口代码给出，未做 Windows 实机验收：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PA_MODEL_BASE_URL="https://你的接口地址/v1"
$env:PA_MODEL_NAME="你的模型名"
$env:PA_MODEL_API_KEY="你的密钥"
.\.venv\Scripts\python.exe run.py
```

Windows 或其他系统需要手机同网访问时，不用只支持 Unix 的守护入口，也不把 `PA_BIND` 误用到 run.py；在已安装依赖且已设置模型变量的环境中显式前台监听：

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

PowerShell 未激活虚拟环境时，把 `python` 替换为 `.\.venv\Scripts\python.exe`。电脑防火墙需允许可信局域网入站；开放监听只用于受控演示，不等于完成公网安全部署。前台入口不会打印守护脚本的二维码地址，启动后在页面「手机访问」读取当前入口；需要时用 `PA_LAN_IP` 指定实际电脑 IP。

## 既有数据与模型配置

`PA_DATA_DIR` 指定运行数据目录，默认作品根目录的 data。应用启动会建库与虚构 seed。既有库中的模型设置优先于环境变量；复用旧库后连到旧接口时，在「⋯ → 演示设置 → 模型连接」检查和修改，不删除已有资料来换模型。

模型设置仅供服务运行者。页面保存密钥后只返回「已设置」状态，密钥仍存于本地运行库；不要分享这个数据库或把密钥贴入公开材料。显示连接失败时，查看错误并重试；协商失败保留事项与原因，不算成功。

「重置演示资料」是破坏性维护动作，只在本人确认不需要全部现有记录、任务历史及设备连接后使用。它会清空任务／协商／资料关联和插件设备状态，新增邀请与来源关联也会丢失；插件需重新配对。不要日常试用时随时重置，不在保存原任务或录制时重置。

当前是有限本地／局域网 guest 演示，不是实名多账号系统或安全公网服务。开发身份切换仅用于隔离验收，不能让发起人代答他人，也不当公开登录方案。

## 手机上打开与录入

首页右上角「⋯ → 演示设置 → 手机访问 → 在手机上打开」提供当前地址、二维码与说明。手机和电脑需在可互访的同一局域网；可扫码或手输当前地址，电脑保持开机和服务运行。手机的 `127.0.0.1` 指手机自身，不是电脑。网络变化后重新取地址，不照抄历史固定 IP。

局域网 HTTP 下，主屏安装、相机与语音权限由具体浏览器决定；Android 真机尚未验证，不承诺全设备可安装或离线业务可用。

录入从「☰ → 资料库 → 添加资料」进入，在「记什么」同一个正文框可以先打字：

- 默认「加一张图」选图／拍照后自动识别整图，结果追加到原正文末尾；核对错字、对象和发生日期后保存。
- 首次只存图片，在选图前点「只作配图」，不调识别。
- 首次只认一部分，在选图前点「先裁剪再识别」，确认后只有所选范围送识别；取消不识别。
- 默认识别后才改成只存配图，不会撤销此前整图已上传的事实。
- 图像压缩长边目标为 1600；实际体积、方向、对焦与触屏裁剪待真机验收。识别失败保留原文与图片，说明原因，可以编辑或重试。

保存本身不等于分享或用于某件事；本人明确选择事项后才按对象、用途和授权采用。家庭活动的限定独立参与和资料更正再次采用已通过 t27 限定独立验证；最终服务版与 Android 仍待核，最新可操作步骤以[README](<../README.md>)和[安卓清单](<delivery/ANDROID-CHECKLIST.md>)为准。

## Chrome 插件与模拟商店

电脑 Chrome 打开 `chrome://extensions`，启用开发者模式，加载[插件目录](<../extensions/context-bridge>)，固定「随身助手」图标。不要求 npm 构建，不是手机 Chrome 可安装的扩展。

另开一个终端，在作品根目录保持模拟商店运行：

```bash
python -m http.server 9000 --bind 127.0.0.1 \
  --directory extensions/context-bridge/fixtures/mock-shop
```

可打开 `http://127.0.0.1:9000/index.html` 或 `http://127.0.0.1:9000/product2.html`。关掉该终端，商店服务就停止。

1. 在主应用「⋯ → 演示设置 → 浏览器插件」取一次性配对码，在插件中输入并连接本人服务。
2. 选择本人购物事项，查看需求、来源与待确认项，换页面复制当前需求；未适配网页由本人手动继续。
3. 有效且已约定的购物事项在支持的模拟商店页面预览参数，由本人确认后代填；插件读回页面实际值。
4. 回到应用同一事项，查看代填回执中的期望值、实际值和复核状态。回执不代表已下单或已付款；家庭活动不进入购物接口。

撤销设备、清浏览器数据、重置演示资料会导致重新连接。插件本轮桌面开发副本通过与应用 HTTP 回执已验，不替代手机端同事项回执的真机操作。

## 测试不是日常启动命令

普通运行只安装 [requirements.txt](<../requirements.txt>) 中的应用依赖。当前源码候选保留核心运行、必要模拟商店与契约 fixture、两个 Node 本地单元，以及限定参与、资料关联、客户端重试、连接器接入、送礼档案、朋友旅行、关系分类与家庭缺口八个测试；不包含全部历史浏览器、探针、拍摄和录制辅助脚本。原件留工程原位，不因排除而删除。最终包内文件以交付清单和逐文件 SHA-256 为准。

保留的后端接口测试额外需要 httpx，Node 单元需要 Node.js；图标重生成需要 Pillow。更广的历史浏览器验证还需要 websockets、Chrome，录像可需 FFmpeg；这些外部二进制、字体及虚拟环境不随源码分发，未验的 FFmpeg 构建不冒称已完成许可证核查。Python 依赖为版本范围，另一台电脑的干净安装、Windows、外部 Chrome 路径仍未验。

[客户端重试单元](<../tests/test_model_client_retry.py>)只用 mock 响应，不连接模型；[限定参与接口](<../tests/test_plan_participation_api.py>)与[资料关联测试](<../tests/test_note_plan_link.py>)在导入应用前自行指定独立 PA_DATA_DIR，并用 stub。先读脚本，在独立进程运行；以下是包内安全子集的调用示例，不代替另一台电脑的实跑验收：

```bash
python -m pip install httpx
python tests/test_model_client_retry.py
python tests/test_plan_participation_api.py
python tests/test_note_plan_link.py
node --test extensions/context-bridge/test/contract.test.js \
  extensions/context-bridge/test/api-renewal.test.js
```

旧 guard、入口、配置、真实 E2E 及历史浏览器／录制脚本均不随本次包分发，原件只留工程原位。原 E2E 没有指定 PA_DATA_DIR 时会回落工程 data，默认路径会运行真实模型全套并产生费用；严禁把其 docstring 中的历史私机虚拟环境路径当普通启动步骤，也不直接执行不带隔离设置与模式选择的 E2E。不要对日常库或生产 8000 批量运行任何历史脚本。包内单元与 stub 通过只说明本地程序行为，真实模型、独立 QA 与 Android 结果见[验证摘要](<delivery/verification-summary.md>)。
