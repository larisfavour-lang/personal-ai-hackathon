# 第三方许可声明（THIRD-PARTY NOTICES）

本文件区分「本项目自有内容」与「第三方依赖」。本项目整体以 MIT 许可发布（见根目录
`LICENSE`，Copyright (c) 2026 larisfavour-lang）；MIT 仅覆盖本项目自有文件，不改变
下列第三方组件各自的开源许可，也不代表本项目重新许可或捆绑分发它们。

## 一、本项目自有内容（MIT，见 LICENSE）

- `app/`：FastAPI 服务端、模型调用封装、协商/规划/资料/参与等服务逻辑与单页前端
  （`app/static/index.html`、`apps.html`、`sw.js`、`manifest.webmanifest`）。
- `extensions/context-bridge/`：浏览器插件源码与仓库内模拟商店夹具（`fixtures/mock-shop/`）。
- `tests/`：测试与验证脚本（本项目编写）。
- `scripts/generate_app_icons.py` 及 `app/static/icon-source.svg`、`app/static/icon-192.png`、
  `app/static/icon-512.png`：2026-10-16 绘制（commit 60ac127，2026-10-16 20:40 +0800）的原创应用图标及其 SVG 源与生成脚本，
  由本项目自行创作，可随本项目 MIT 授权再分发；同环境重生成 PNG 与入库文件字节一致。
- `docs/`、`README.md`、演示脚本：本项目撰写。演示录像与界面截图：经确认为本项目录制的素材；**权利不明或非本人录制的旧资产不在此列**（不构成概括授权，以交付与公开前的最终清单为准）。
- 演示数据（人物、事项、商品等）均为标注的虚构测试数据。

## 二、第三方依赖（pip 安装，许可随各自分发包保留）

本项目**不**随源码包分发第三方依赖的源码、wheel 或二进制；运行与测试时由
`pip install` 自行获取，各组件的许可以其发行包内许可文本为准。下列许可信息在本机
已安装发行包的许可原文核对（2026-10-17，Python 3.11 环境）。

### 运行必需

| 组件 | 观测版本 | 许可 | 上游 |
| --- | --- | --- | --- |
| FastAPI | 0.143.0 | MIT（Copyright (c) 2018 Sebastián Ramírez） | https://github.com/fastapi/fastapi |
| Uvicorn | 0.54.0 | BSD-3-Clause（Copyright © 2017-present Encode OSS Ltd） | https://github.com/encode/uvicorn |
| Pydantic | 2.14.1 | MIT（Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors） | https://github.com/pydantic/pydantic |
| qrcode | 8.2 | **BSD-3-Clause 主体**（Copyright (c) 2011, Lincoln Loop），并附内置旧来源 MIT 许可注记（Kazuhiko Arase 的 JavaScript 实现）；**不得简化表述为纯 MIT** | https://github.com/lincolnloop/python-qrcode |

> "QR Code" 是 DENSO WAVE INCORPORATED 的注册商标（见 qrcode 发行许可注记）。

### 测试额外需要（不随运行分发，不分发二进制）

| 组件 | 观测版本 | 许可 | 上游 |
| --- | --- | --- | --- |
| Pillow | 12.3.0 | **MIT-CMU**（Python Imaging Library，Copyright © 1997-2011 by Secret Labs AB；Copyright © 1995-2011 by Fredrik Lundh）。上游 wheel 另捆绑 FreeType、HarfBuzz、LCMS2、libpng、libjpeg、libtiff、libwebp、libyuv、OpenJPEG、RAQM、zlib、Zstd 等库并各带其许可；**本项目不分发 Pillow 二进制，上述捆绑库许可在此仅作提示，不随本包复制** | https://github.com/python-pillow/Pillow |
| websockets | 17.2 | BSD-3-Clause（Copyright (c) Aymeric Augustin and contributors） | https://github.com/python-websockets/websockets |
| httpx | 0.28.1 | BSD-3-Clause（Copyright © 2019 Encode OSS Ltd） | https://github.com/encode/httpx |

### 可选录像工具（不随源码分发）

| 组件 | 观测版本 | 许可 | 上游 |
| --- | --- | --- | --- |
| imageio-ffmpeg | 0.6.0 | 包元数据 BSD-2-Clause；其依赖的 FFmpeg 二进制构建许可未核验，**不随本项目分发** | https://github.com/imageio/imageio-ffmpeg |

### 传递依赖（安装时自动解析，许可各自保留）

安装上述组件时会自动带入传递依赖，包括但不限于：Starlette（BSD-3-Clause）、
pydantic-core（MIT）、httptools（MIT，附 http-parser/llhttp 许可）、uvloop
（MIT / Apache-2.0 双许可，二选一）、watchfiles（MIT）、PyYAML（MIT）、
python-dotenv（BSD-3-Clause）、anyio（MIT）、certifi（MPL-2.0）、click
（BSD-3-Clause）等。完整名单与许可以实际安装环境的包元数据为准；区间安装产生的
新版本不自动视为已核验。

## 三、边界

- 本文件不构成法律意见；组件版本以 `requirements.txt` 与实际安装环境为准。
- 未随本包分发的第三方二进制（Pillow 及其捆绑库、FFmpeg）的许可文本不在此复制，
  需要时以上游发行包为准。
- 权利存疑的素材（非本人录制的截图/音视频/字体等）不进入交付与公开快照；
  公开文件集以发布前的最终清单与哈希为准。
- 模型服务由运行者自备：作品经 OpenAI 兼容接口调用（默认本地代理转
  `step/step-5-preview`），接口方许可与计费由运行者与接口方承担，与本项目
  MIT 许可无关。
