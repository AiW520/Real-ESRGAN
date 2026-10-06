# Real-ESRGAN AI Image Upscaler

本项目是可运行的 FastAPI + React/Vite + PyTorch Real-ESRGAN 图片超分辨率应用。依赖、模型缓存、上传临时文件、浏览器和结果全部位于 `D:\图片超分`。

生产接入已确定为「生成平台后端调用的内部服务」。认证、部署配置、请求去重、失败恢复、监控与回滚见 [DEPLOYMENT.md](DEPLOYMENT.md)。开发工作台与生产 API 使用不同启动配置；生产默认关闭网页和内置生图。

## 启动

从仓库获取源码（安装及产物保留在 D 盘）：

```powershell
git clone https://github.com/AiW520/Real-ESRGAN.git 'D:\图片超分'
```

仓库包含源码、测试和部署文档，不包含模型权重、凭据、运行数据或本机质量报告。官方模型会在首次使用或部署预热时下载；质量报告可用 `scripts/validate_quality.py` 等验证脚本重新生成。RRDB 实现与官方模型来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

首次安装或重建环境，在 PowerShell 执行：

```powershell
Set-Location 'D:\图片超分'
.\setup.ps1
```

启动服务：

```powershell
Set-Location 'D:\图片超分'
.\start.ps1
```

首次处理会从 Real-ESRGAN 官方 GitHub release 下载并缓存模型到 `models\`。打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/) 。

## API 测试

```powershell
curl.exe -F "file=@D:\input\photo.jpg" -F "scale=2" -F "resolution=scale" -F "output_format=png" http://127.0.0.1:8000/api/upscale
```

返回 `job_id` 后轮询 `/api/upscale/{job_id}/status`，完成后访问 `/api/upscale/{job_id}/download`。4K→8K 示例：

```powershell
curl.exe -F "file=@D:\input\4k.jpg" -F "scale=4" -F "resolution=8K" -F "output_format=png" http://127.0.0.1:8000/api/upscale
```

也可以使用 CLI，状态会按 tile 输出到终端：

```powershell
.\.venv\Scripts\python.exe -m backend.cli D:\input\4k.jpg --output D:\图片超分\data\validation\4k-to-8k.png --scale 2 --resolution 8K --tile-size 256
```

测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Push-Location frontend; npm.cmd run build; Pop-Location
```

2026-10-06 复核：68 项测试通过，前端构建通过；隔离生产实例的真实 AI 超分、两种缩图、认证下载及重启去重通过。当前可交付服务器部署验收；目标服务器性能与生成平台业务接入仍需在对应环境验证。

## 生成并超分

工作台中的「AI 生成」支持提示词生成基础图，然后复用上传模式的 Real-ESRGAN 处理器输出 2K/4K；选择「基础图」会直接编码生成结果，不运行超分。生成请求使用固定 `gpt-image-2` 与 `https://api.tu-zi.com/coding/images/generations`。

`scripts/tuzi_generate_image.py` 是已安装 Tuzi 技能脚本的原样副本。可通过服务器环境变量 `UPSCALER_TUZI_SCRIPT` 指向该脚本的绝对路径。凭据由脚本读取：优先 `TUZI_CODING_API_KEY`，其次当前服务进程的 `OPENAI_API_KEY`，然后 API Key 模式的 Codex 凭据。服务器部署时配置服务器自身的凭据。不要把凭据写入前端或提交到源码中。

- `GET /api/generation/config` 检查配置，不产生生成请求。
- `POST /api/generate` 接收 JSON：`prompt`、`aspect`（square/landscape/portrait）、`output_level`（native/2K/4K）、`model`、`output_format`、`quality`。
- 继续使用 `/api/upscale/{job_id}/status` 与 `/download`；`/base` 返回生成基础图。
- `POST /api/upscale/{job_id}/continue` 复用已保存的基础图，接收 `output_level`（2K/4K）、`model`、`output_format`、`quality`。此接口不检查生成凭据、不调用生成服务；从失败任务或基础图输出任务都能继续超分。
- 有效基础图在超分失败、取消或服务重启后保留至任务过期；续处理将复制为新任务的独立基础图，原任务过期不会删除新任务的底图。刷新工作台可恢复最近一次生成/续处理任务，浏览器只保存任务 ID。
- 方形基础图请求 1024×1024，横向请求 1536×1024，竖向请求 1024×1536；以实际返回尺寸为准。这不是原生 1080p。
- 预设保留比例并限制总像素：方形 2K=1920×1920、4K=2880×2880，不强制拉伸成 16:9。生成模式暂不提供超过 4 倍上限的 8K。
- 不自动重试生成；失败、超时和取消也可能已产生上游费用。「再次生成」会创建新的付费生成请求。任务文件不保存提示词。
- 开发工作台仅监听本机地址。生产模式使用服务令牌和提交限速，并默认关闭生成入口；终端用户认证、调用额度和图片归属由生成平台后端负责。

本轮验证：本地测试基础图 1024×1024 → 2880×2880，CPU `anime_6B`，25 tile，约 238 秒。报告在 `data/validation/generation-local/report.json`。真实 Tuzi 请求返回 502，尚未验证成功的在线生成到超分链路。该本地测试没有调用生成 API。

续处理验证：浏览器从保存的基础图提交 `/continue`，真实 CPU `anime_6B` 推理，1024×1024 → 1920×1920，25 tile，约 221 秒；处理中刷新、桌面/手机结果恢复、对比和下载通过，生成接口调用次数为 0。报告与截图位于 `data/validation/continue/`。从外部生成平台接入与公网部署尚未在目标服务器验证。

浏览器测试默认禁止付费请求；使用 `GENERATION_LOCAL_JOB` 可回放本地已完成任务。只有显式设置 `TUZI_LIVE_VERIFY=1` 才能通过 `frontend/scripts/generation-smoke.mjs` 发起单次在线验证，请勿自动重试。

## 接入已有生成平台

### 统一后处理接口

`POST /api/upscale` 的 `processing_mode` 支持：

| 值 | 行为 | scale / resolution | 是否运行模型 |
|---|---|---|---|
| `upscale`（默认） | AI 放大或原尺寸增强 | 1/2/4 倍或 original/2K/4K/8K | 是 |
| `resize` | Lanczos 保真缩图，透明图预乘 Alpha 缩放 | scale=0.5/0.25，resolution=scale | 否 |
| `ai_resize` | Real-ESRGAN 分块推理后缩至目标尺寸 | scale=0.5/0.25，resolution=scale | 是 |

缩图 API 还支持按原比例指定 `target_width` 或 `target_height`，不能通过缩图模式放大。`/api/generate` 与 `/continue` 使用相同的 `processing_mode`，并用 `shrink_scale=0.5/0.25` 指定缩小比例。模式和实际输出尺寸返回在任务状态中。

AI 增强缩图在输入尺寸执行模型推理，耗时不会按最终小图面积相应减少。纯文字、Logo、人脸身份等严格保真场景，应核对原图；AI 可能重构内容，不能承诺完美还原。

可直接使用 `backend/client.py` 的平台后端适配器（不自动重试提交）：

```python
import os
from backend.client import UpscalerClient

with UpscalerClient('http://127.0.0.1:8000', api_token=os.environ.get('UPSCALER_API_TOKEN')) as upscaler:
    # 此处的 generated.png 是你的生成平台已取得的基础图。
    job_id = upscaler.submit('generated.png', mode='upscale', resolution='4K',
                             request_id='platform-image-123-upscale-v1')
    # 立即把 job_id 保存到平台数据库。断线后对同一个 ID 调用 wait/status。
    result = upscaler.wait(job_id, timeout=1800)
    upscaler.download(job_id, 'result-4k.png')
    # 缩图模式示例：submit('generated.png', mode='resize', scale=0.5)
    # AI 缩图示例：submit('generated.png', mode='ai_resize', scale=0.5)
```

质量实测与可视化对比在 `data/validation/quality-audit/`。照片模型真实 960×540 → 3840×2160，CPU 约 381 秒；照片、文字、透明通道、分块接缝及缩图均有独立测试。此次未调用生成 API，不能据此声称 Tuzi 上游 502 已恢复。

### 原始 HTTP 调用示例

如果其他生成服务已取得低分辨率图片，应由它的后端把图片文件直接提交给 `/api/upscale`，等待终态，再向用户返回下载结果。无需为了超分重新调用本项目的生成接口。下例仅用于本机开发；生产使用上面的认证客户端并持久化业务请求 ID，完整错误处理见部署手册。

```python
import time
import httpx

def upscale_generated_image(base_path, output_path, service_url):
    with httpx.Client(base_url=service_url, timeout=120) as client:
        with open(base_path, 'rb') as image:
            response = client.post('/api/upscale', files={'file': image}, data={
                'resolution': '4K', 'model': 'RealESRGAN_x4plus', 'output_format': 'png',
            })
        response.raise_for_status()
        job_id = response.json()['job_id']  # 保存此 ID，断线后查询同一任务，避免重复提交。
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            status_response = client.get(f'/api/upscale/{job_id}/status')
            status_response.raise_for_status()
            status = status_response.json()
            if status['status'] == 'completed':
                result = client.get(f'/api/upscale/{job_id}/download')
                result.raise_for_status()
                with open(output_path, 'wb') as target:
                    target.write(result.content)
                return status
            if status['status'] in {'failed', 'cancelled'}:
                raise RuntimeError(status.get('error') or status['status'])
            time.sleep(2)
        raise TimeoutError(f'任务仍未完成；继续查询任务 {job_id}，不要重新生成底图。')
```

当前队列是单 worker、最多 4 个活动任务，需使用 `uvicorn --workers 1`，不能直接增加 Web worker 数量。生产吞吐量与服务器 GPU/内存有关，本机 CPU 实测不代表 CUDA 或目标服务器性能。服务器必须持久化 `data/` 和 `models/`，并保护内部调用接口；公网账号、额度和限流方案需按上线方式确定。

## 工作台交互（1.2）

- 导入：拖入、点击选择，或在页面任意位置按 Ctrl+V 粘贴截图；Ctrl+Enter 开始处理。
- 提交前按后端同样的规则检查目标尺寸（4 倍上限、33.2 MP / 7680 px、原图大于预设），不合法时直接提示并禁用按钮，不再上传后才报错。
- 处理中显示分块进度格、已用时间和按实测分块速度推算的剩余时间；浏览器标签页标题显示百分比；上传任务处理中刷新/关闭页面会先确认。
- 后端英文错误在工作台中显示为中文并给出下一步；状态轮询在断线时指数退避（最长 15 秒），标签页隐藏时降频。
- 对比器：以光标为中心滚轮缩放（最高 800%），≥200% 时按像素显示；双击切换 100%/适应窗口；聚焦后方向键平移、`+`/`-`/`0`/`1` 缩放；透明区域显示棋盘格；预览区高度随窗口变化。
- 下载文件名改为「原文件名_宽x高_upscaled.png」（缩图为 `_resized` / `_ai-resized`）。
- 状态接口新增 `started_at`（开始处理时间）和 `queue_position`（排队位置，0 表示不在排队）。
- CPU 推理线程默认使用物理核心数（原固定为 4），仍可用 `UPSCALER_THREADS` 覆盖。

## 超分实现边界

- 模型是官方 RealESRGAN_x4plus（23 个 RRDB block）或 anime_6B，按 tile 运行并对 overlap 做渐变权重融合；目标尺寸通过高质量 Lanczos 做最后精确校正。
- CUDA 可用时自动使用 CUDA/FP16；Apple MPS 和 CPU 也可运行。CUDA OOM 会将 tile 依次减半到 128px。
- Alpha 单独保留并使用高质量缩放后合成；ICC 输入转换为 sRGB 并写入输出。
- 当前本机为 CPU PyTorch + AMD 集成显卡，因此 4K→8K 速度受硬件限制；本次实测 3840×2160 → 7680×4320 使用 144 tile、CPU、anime_6B 耗时约 22.7 分钟。NVIDIA CUDA 会自动启用 FP16，并在 OOM 后将 tile 依次降级到 128/64px，再回退 CPU。
- 人脸增强默认关闭；开启时下载并缓存官方 GFPGAN、RetinaFace 检测和 ParseNet 解析权重，实际会改变部分面部像素。
- 模型权重在上游未提供 SHA-256 时生成本地 provenance manifest；已有无 manifest 的同名文件会被拒绝使用。
