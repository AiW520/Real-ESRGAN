# 生成平台内部超分服务：上线手册

发布版本 1.1.0。选定的接入方式是生成平台后端调用；平台取得基础图后上传文件，异步查询任务，下载高分辨率结果，再存入平台自己的图片存储。前端继续调用平台后端。

## 1. 当前交付边界

- 已在本机 Windows / Python 3.11 / CPU PyTorch 上运行生产模式，真实验证三种处理模式、令牌认证、下载、请求去重和重启恢复。
- 单进程、单推理 worker；默认最多 4 个活动任务，包含排队任务。每个数据目录只能运行一个实例，文件锁会阻止重复启动。
- 服务器系统、GPU/显存、内存以及生成平台源码尚未提供，目标服务器安装、吞吐量、平台数据库接入仍须实机验收。没有部署到公网；没有验证 Docker 镜像。
- 本机照片 960×540 → 3840×2160 的真实 CPU 耗时约 381 秒。不要把 HTTP 请求一直挂到推理完成，也不要把排队容量当成并行处理数。

## 2. 安装与环境

在 D 盘解压发布包。Windows 使用已有 Python 3.11 在 D 盘创建 `.venv`，将 `TEMP`、`TMP`、`PIP_CACHE_DIR` 指向 D 盘后安装 `requirements-runtime.txt`。已有本项目环境可直接使用，不需重复安装。

```powershell
Set-Location 'D:\图片超分'
$env:TEMP = "$PWD\.cache\tmp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "$PWD\.cache\pip"
New-Item -ItemType Directory -Force $env:TEMP,$env:PIP_CACHE_DIR | Out-Null
# 仅当 .venv 不存在时执行：py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
Copy-Item .env.example .env.production
```

在服务器上编辑 `.env.production`，填写随机服务令牌（至少 32 个 ASCII 字符，推荐使用 `secrets.token_urlsafe(48)`），通过服务器密钥管理工具保存并限制文件读权限。示例中的空令牌会阻止启动。不要复制本机生成服务的 API Key。

`requirements-runtime.txt` 固定直接依赖及 Starlette 版本；`environment-local.json` 记录本机已验证依赖。它不是 Linux/CUDA 的完整锁文件。NVIDIA 服务器需要按驱动选用匹配的官方 PyTorch CUDA wheel，并再次执行下面的模型预热及真实测试。

`.env.production` 不会自动加载；使用 `--env-file` 或由服务管理器注入变量。已有进程环境变量优先于文件。运行目录下的 `.cache`、`models`、`data` 默认均在 D 盘；如指定自定义路径，也应放在 D 盘。服务器系统未确定前不套用其他盘符或 Linux 路径。

## 3. 配置与启动

| 配置项 | 默认值 / 作用 |
|---|---|
| `UPSCALER_PRODUCTION` | 上线必须为 `1`；关闭网页、API 文档入口 |
| `UPSCALER_API_TOKEN` | 必填；仅保存在平台后端和超分服务 |
| `UPSCALER_ALLOWED_HOSTS` | `127.0.0.1,localhost`；跨机器添加实际内部主机名或 IP，不含端口 |
| `UPSCALER_ENABLE_GENERATION` | 生产默认 `0`；平台直接上传已生成图片 |
| `UPSCALER_MAX_JOBS` | `4`；活动任务上限，满额返回 429 |
| `UPSCALER_SUBMISSIONS_PER_MINUTE` | `30`；单实例提交/检查请求限速，重启清零 |
| `UPSCALER_TIMEOUT` | `21600` 秒；处理超时，在分块检查点协作停止 |
| `UPSCALER_TTL` | `86400` 秒；终态任务和结果保留时间 |
| `UPSCALER_THREADS` | `4`；CPU 推理线程数 |
| `UPSCALER_DATA_DIR` / `MODELS_DIR` / `CACHE_DIR` | 后两项也使用 `UPSCALER_` 前缀；设置持久化路径 |

先准备并校验官方模型；此步骤可能下载权重，不调用付费生图：

```powershell
.\.venv\Scripts\python.exe scripts/preflight.py --env-file .env.production --warm-models --output data/preflight.json
.\start-production.ps1 -EnvFile .env.production
```

默认监听 `127.0.0.1`，适合与平台后端在同一服务器。跨机器时使用 `-BindAddress <服务器内网IP>`，同时设置允许主机名、防火墙仅允许平台服务器访问；跨网络传输通过 HTTPS/TLS 代理或安全隧道。不要将无 TLS 的令牌请求暴露到公网。

由目标系统的服务管理器负责自启动、日志轮转、异常重启和环境注入。只启动一个 `python -m backend.serve`，不要使用 `--reload` 或增加 Uvicorn worker。扩容需要独立数据目录和任务路由，当前版本不支持任意实例间共享任务查询。

## 4. 平台后端接入

使用包内 `backend/client.py`，客户端仅需 `httpx`。业务任务必须持久化基础图、处理参数、稳定请求 ID 和返回的 job_id。先保存请求 ID，再提交；发生网络超时后，用相同文件、相同参数和相同 ID 重试。

```python
import os
from backend.client import UpscalerClient

# request_id 来自平台数据库，同一业务处理请求跨重试保持不变。
request_id = 'platform-image-123-upscale-v1'
with UpscalerClient(os.environ['UPSCALER_URL'],
                    api_token=os.environ['UPSCALER_API_TOKEN']) as client:
    job_id = client.submit('generated.png', resolution='4K', request_id=request_id)
    # 此处立即将 job_id 保存到平台数据库；下列步骤应放在平台后台任务中。
    result = client.wait(job_id, timeout=1800)
    client.download(job_id, 'result-4k.png')
    # 将文件上传平台存储，保存实际 output_width/output_height，再通知用户。
```

客户端默认不读取系统 HTTP 代理，避免内网请求携带服务令牌走意外的代理。如需受控代理/企业证书环境，应明确配置并验证，`trust_env=True` 可启用环境配置。

| 需求 | `submit` 参数 |
|---|---|
| AI 超分 | `mode='upscale', resolution='4K'` |
| 精确 2 倍 | `mode='upscale', resolution='scale', scale=2` |
| 保真缩图 | `mode='resize', scale=0.5` |
| AI 增强后缩图 | `mode='ai_resize', scale=0.5` |

`POST /api/upscale` 返回 job_id；`GET /api/upscale/{job_id}/status` 查询；完成后 `GET /api/upscale/{job_id}/download` 下载。三者都要求 `Authorization: Bearer <token>`。浏览器不能直接持有服务令牌或访问这些下载地址，平台应校验用户归属后代理下载或签发自己存储的下载链接。

每次提交可携带 `Idempotency-Key`（8–128 个字母、数字或 `._:-`）。同一键和内容返回原任务，内容或参数变化返回 409。原任务失败也不会自动重跑：平台确认需要新一次处理后，使用新的业务请求 ID。去重只在任务记录的保留期内有效，超过 TTL 后平台自身的数据库负责防重复。

- 401：检查令牌；不要在错误日志中打印请求头。
- 400/413/422：修正格式、尺寸、参数或上传体积；上传最大 50 MiB，输出最大 33.2 MP / 最长边 7680。
- 409：请求 ID 被用于不同参数，禁止用同一个 ID 更改任务。
- 429：队列或限速已满。遵循 `Retry-After`（若有），使用相同 ID 做有上限的指数退避；不要重新生成基础图。
- 网络超时/5xx：先保留原图与请求 ID；轮询已知 job_id，或按相同提交内容查询去重结果。
- `failed`/`cancelled`：向平台记录原因。客户端 wait 超时不代表服务端已取消，应继续查询同一任务。
- 重启中断的任务标记失败，不静默重放；普通上传任务由平台保留原图后重提。内置生成任务的已保存基础图可用 `/continue` 续处理，不产生生图调用。
- 下载结果要在 TTL 内转存至平台存储；本服务的临时目录不是长期相册。

一个服务令牌代表一个可信后端，不隔离终端用户。用户身份、余额/额度、图片归属和业务计费由平台维护。本地内置 Tuzi 生图曾返回 502；当前选定的上线链路不依赖该入口，不能据本地超分测试声称上游已恢复。

## 5. 监控、升级与回滚

- `/healthz`：进程响应；`/readyz`：持有任务目录锁且未关闭。两者无需令牌，仅返回布尔状态。模型是否齐备须由 preflight 检查，ready 不代表模型已预热。
- `/api/operations`：需令牌，查看各阶段任务数量与关闭状态。平台关注任务时长、失败率、429 数量，主机监控内存/显存和 D 盘空间。
- 预检要求至少 2 GiB 磁盘及 1 GiB 可用内存，只是基础门槛。大图还会逐请求检查资源；上线按实际输入尺寸和排队量做容量测试。
- 正常升级前停止新提交、等待队列结束，再停止进程并备份 data 的一致性快照、配置及模型 manifest。将运行目录与版本目录分开，保留旧发布包。
- 验证新版本健康与真实样例后再恢复平台流量。失败时停止新实例、恢复旧代码/环境及升级前一致性数据快照；不要两个版本同时打开同一数据目录。强制停止会让进行中的任务失败。
- 结果写入时先完成校验再标记 completed。客户端采用 `.part` 下载并原子重命名，防止平台把半张图片当成成功。

## 6. 验收

```powershell
# 使用隔离数据目录和随机测试令牌，不修改当前服务，无付费生成。
.\.venv\Scripts\python.exe scripts/validate_production.py
# 完整测试另需 requirements.txt 中的开发依赖。
.\.venv\Scripts\python.exe -m pytest -q
```

生产实测脚本覆盖真实模型推理、普通缩图、AI 缩图、RGBA、认证下载、错误 Host、网页关闭、内置生成关闭和进程重启后去重。报告写入 `data/validation/production/<运行ID>/`，不记录令牌。它使用小型合成数据验证链路；质量评估仍以真实照片/文字对比报告为依据。

最终上线还须在目标服务器用平台实际生成的照片、人像、文字、Logo、透明图，逐一检查 2K/4K 实际尺寸、颜色、边缘和细节，并测试并发排队/超时。AI 可以改变文字与脸部细节，不能承诺无损恢复；人脸增强默认关闭，严格保真缩图选 `resize`。4K 预设保留比例并限制像素数，16:9 为 3840×2160，方形为 2880×2880，平台应展示真实尺寸。
