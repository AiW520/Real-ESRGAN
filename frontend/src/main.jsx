import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Download,
  Minus,
  Plus,
  Maximize2,
  Scan,
  Sparkles,
  Upload,
  X,
  MoveHorizontal,
  Image as ImageIcon,
  SlidersHorizontal,
  Check,
  ArrowUpRight,
  Layers,
  TriangleAlert,
  Clock,
} from "lucide-react";
import "./styles.css";

const API = "";
const modeNames = {upscale: 'AI 超分', resize: '保真缩图', ai_resize: 'AI 增强缩图'};
const MAX_OUTPUT_PIXELS = 33177600, MAX_EDGE = 7680;
const ACTIVE = ["queued", "generating", "loading_model", "processing", "merging", "encoding"];

// Backend errors are English for API clients; the workbench shows them in Chinese with a next step.
const errorMessages = [
  [/queue is full/i, '队列已满，请等待当前任务完成后再提交。'],
  [/worker is stopping/i, '引擎正在停止，请等待服务重新就绪。'],
  [/exceeds the 50 MB/i, '文件超过 50 MB 上传限制。'],
  [/33\.2 MP|longest-edge/i, '输出超过 33.2 MP 或 7680 px 长边上限，请降低倍率或改用 8K 预设。'],
  [/exceeds 4x/i, '目标尺寸超过 4 倍放大上限，请选择更小的目标分辨率。'],
  [/smaller than the input/i, '目标尺寸小于原图，请选择「原尺寸增强」或更大的目标分辨率。'],
  [/even with 64px tiles/i, '即使使用 64px 分块内存仍不足，请关闭其他程序或换一张更小的图片。'],
  [/Insufficient RAM/i, '可用内存不足，请关闭其他程序或换一张更小的图片。'],
  [/Insufficient D-drive space/i, 'D 盘剩余空间不足，请清理磁盘后重试。'],
  [/16-bit|HDR/i, '暂不支持 16 位或 HDR 图片，请先转换为 8 位 SDR。'],
  [/Animated or multi-page/i, '不支持动图或多页图片。'],
  [/damaged|not a valid image|pixel limit/i, '图片已损坏、格式无效，或像素数超过 33.2 MP 上限。'],
  [/ICC profile/i, '内嵌 ICC 色彩配置无法安全转换为 sRGB，请导出为 sRGB 后重试。'],
  [/cancelled by the user/i, '任务已取消。'],
  [/server restarted/i, '服务重启时任务尚未完成。如有基础图，可从基础图继续超分。'],
  [/processing timeout/i, '处理超时，请换一张更小的图片或使用 CUDA 设备。'],
  [/Unable to download/i, '模型下载失败，请检查网络后重试。'],
  [/Idempotency-Key/i, '重复提交的参数不一致，请刷新页面后重新提交。'],
  [/rate limit/i, '提交过于频繁，请稍后再试。'],
  [/Failed to fetch|NetworkError|Load failed/i, '无法连接本地引擎，请确认服务仍在运行。'],
];
const translateError = (message) => {
  if (!message) return message;
  const match = errorMessages.find(([pattern]) => pattern.test(message));
  return match ? match[1] : message;
};

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return { detail: `服务返回 ${response.status}，响应无法解析。` };
  }
}

function stageLabel(job) {
  const stage = job.current_stage || "";
  switch (job.status) {
    case "queued":
      return job.queue_position > 1 ? `排队中 · 前面还有 ${job.queue_position - 1} 个任务` : "排队中";
    case "generating": return "生成基础图";
    case "loading_model": return stage.startsWith("Validating") ? "校验图片" : "加载 AI 超分模型";
    case "merging": return "合并分块细节";
    case "encoding": return "编码图片";
    case "processing":
      if (/Memory exhausted/i.test(stage)) return "内存不足，正在用更小的分块重试";
      if (/switching to CPU/i.test(stage)) return "加速器不可用，已切换到 CPU";
      if (/downsampling/i.test(stage)) return "高质量缩图";
      if (/Face enhancement: (\d+) \/ (\d+)/.test(stage)) return stage.replace(/Face enhancement:/, "人脸增强 ·");
      if (/face enhancement/i.test(stage)) return "人脸增强";
      if (job.tiles_total) return `AI 超分 · ${job.tiles_done}/${job.tiles_total}`;
      return "AI 处理中";
    default: return stage;
  }
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60), rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function formatBytes(bytes) {
  if (!bytes) return "0 KB";
  return bytes < 1048576 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}

// Mirrors backend/app/image_processor.target_size limits so impossible jobs are blocked before upload.
function outputProblem(file, output, processingMode, resolution) {
  if (!file || !output) return "";
  if (output[0] * output[1] > MAX_OUTPUT_PIXELS || Math.max(...output) > MAX_EDGE)
    return "输出超过 33.2 MP 或 7680 px 长边上限，请降低倍率或改用 8K 预设。";
  if (processingMode === "upscale" && ["2K", "4K", "8K"].includes(resolution)) {
    const factor = output[0] / file.width;
    if (factor > 4 + 1 / Math.min(file.width, file.height))
      return `原图较小，${resolution} 需要放大 ${factor.toFixed(1)} 倍，超过 4 倍上限。请选择更低的目标分辨率。`;
    if (factor < 1 - 1 / Math.min(file.width, file.height))
      return `原图已大于 ${resolution}，请选择「原尺寸增强」或更高的目标分辨率。`;
  }
  return "";
}

// Chooses a rows × cols layout whose product equals the real tile count and whose shape follows the image.
function tileGrid(total, width, height) {
  const ratio = (width || 1) / (height || 1);
  let best = [total, 1];
  for (let cols = 1; cols <= total; cols++) {
    if (total % cols) continue;
    const rows = total / cols;
    if (Math.abs(Math.log(cols / rows / ratio)) < Math.abs(Math.log(best[0] / best[1] / ratio))) best = [cols, rows];
  }
  return best;
}

function TileMap({ done, total, width, height }) {
  if (!total || total > 400) return null;
  const [cols, rows] = tileGrid(total, width, height);
  const cell = Math.max(4, Math.min(16, Math.floor(220 / Math.max(cols, rows * 1.4))));
  return (
    <div className="tile-map" aria-hidden="true" style={{ gridTemplateColumns: `repeat(${cols}, ${cell}px)`, gridAutoRows: `${cell}px` }}>
      {Array.from({ length: total }, (_, index) => (
        <i key={index} className={index < done ? "done" : index === done ? "current" : ""} />
      ))}
    </div>
  );
}
const presets = {
  "2K": [2560, 3686400],
  "4K": [3840, 8294400],
  "8K": [7680, 33177600],
};

function targetSize(file, scale, resolution) {
  if (!file) return null;
  if (resolution === "original") return [file.width, file.height];
  if (resolution === "scale")
    return [Math.round(file.width * scale), Math.round(file.height * scale)];
  const factor = Math.min(
    presets[resolution][0] / Math.max(file.width, file.height),
    Math.sqrt(presets[resolution][1] / (file.width * file.height)),
  );
  const dimensions = [
    Math.max(1, Math.round(file.width * factor)),
    Math.max(1, Math.round(file.height * factor)),
  ];
  if (dimensions[0] * dimensions[1] > presets[resolution][1])
    return [Math.floor(file.width * factor), Math.floor(file.height * factor)];
  return dimensions;
}

function Comparator({ source, job }) {
  const ref = useRef(null),
    gesture = useRef(null);
  const [size, setSize] = useState({ width: 800, height: 500 });
  const [zoom, setZoom] = useState(null),
    [pan, setPan] = useState({ x: 0, y: 0 });
  const [split, setSplit] = useState(50),
    [mode, setMode] = useState("compare");
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) =>
      setSize({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      }),
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  const fit = Math.min(
    size.width / job.output_width,
    size.height / job.output_height,
  );
  const actual = zoom ?? fit;
  const view = useRef({ actual, pan });
  view.current = { actual, pan };
  useEffect(() => {
    const viewport = ref.current;
    // Zoom around the cursor so the detail being inspected stays under the pointer.
    const wheel = (e) => {
      e.preventDefault();
      const { actual: current, pan: offset } = view.current;
      const next = Math.max(0.025, Math.min(8, current * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      const box = viewport.getBoundingClientRect();
      const cx = e.clientX - box.left - box.width / 2, cy = e.clientY - box.top - box.height / 2;
      const ratio = next / current;
      setZoom(next);
      setPan({ x: cx - (cx - offset.x) * ratio, y: cy - (cy - offset.y) * ratio });
    };
    viewport.addEventListener("wheel", wheel, { passive: false });
    return () => viewport.removeEventListener("wheel", wheel);
  }, []);
  const imageStyle = {
    width: job.output_width * actual,
    height: job.output_height * actual,
    transform: `translate(-50%, -50%) translate(${pan.x}px, ${pan.y}px)`,
  };
  const setRatio = (ratio) => {
    setZoom(Math.max(0.025, Math.min(8, ratio)));
    setPan({ x: 0, y: 0 });
  };
  const fitView = () => {
    setZoom(null);
    setPan({ x: 0, y: 0 });
  };
  const keyboard = (e) => {
    if (e.target.tagName === "INPUT") return;
    const step = 40, keys = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (keys[e.key]) {
      e.preventDefault();
      setPan((p) => ({ x: p.x + keys[e.key][0], y: p.y + keys[e.key][1] }));
    } else if (e.key === "+" || e.key === "=") setRatio(actual * 1.25);
    else if (e.key === "-") setRatio(actual / 1.25);
    else if (e.key === "0") fitView();
    else if (e.key === "1") setRatio(1);
  };
  const move = (e) => {
    const g = gesture.current;
    if (!g) return;
    if (g.type === "split") {
      const box = ref.current.getBoundingClientRect();
      setSplit(
        Math.max(0, Math.min(100, ((e.clientX - box.left) / box.width) * 100)),
      );
    } else
      setPan({
        x: g.x + e.clientX - g.clientX,
        y: g.y + e.clientY - g.clientY,
      });
  };
  return (
    <>
      <div className="compare-toolbar">
        <div className="segments">
          {[
            ["compare", "对比"],
            ["original", "原图"],
            ["result", "结果"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={mode === value ? "active" : ""}
              aria-pressed={mode === value}
              onClick={() => setMode(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="zoom-tools">
          <button title="缩小" aria-label="缩小" onClick={() => setRatio(actual / 1.25)}>
            <Minus size={16} />
          </button>
          <output aria-label="当前缩放">{Math.round(actual * 100)}%</output>
          <button title="放大" aria-label="放大" onClick={() => setRatio(actual * 1.25)}>
            <Plus size={16} />
          </button>
          <button title="适应窗口" aria-label="适应窗口" onClick={fitView}>
            <Maximize2 size={16} />
          </button>
          <button
            title="100% 像素查看"
            aria-label="100% 像素查看"
            onClick={() => setRatio(1)}
          >
            <Scan size={17} />
          </button>
        </div>
      </div>
      <div
        ref={ref}
        className={`compare-viewport ${actual >= 2 ? "pixel-zoom" : ""}`}
        tabIndex={0}
        role="group"
        aria-label="结果预览：拖动平移，滚轮缩放，双击切换 100%，方向键平移，0 适应窗口"
        onKeyDown={keyboard}
        onDoubleClick={(e) => {
          if (e.target.closest(".split-handle")) return;
          Math.abs(actual - 1) < 0.01 ? fitView() : setRatio(1);
        }}
        onPointerDown={(e) => {
          e.preventDefault();
          e.currentTarget.setPointerCapture(e.pointerId);
          gesture.current = {
            type: e.target.closest(".split-handle") ? "split" : "pan",
            clientX: e.clientX,
            clientY: e.clientY,
            ...pan,
          };
          move(e);
        }}
        onPointerMove={move}
        onPointerUp={() => (gesture.current = null)}
        onPointerCancel={() => (gesture.current = null)}
      >
        {mode !== "original" && (
          <img
            draggable="false"
            className="compare-image"
            style={imageStyle}
            src={`/api/upscale/${job.job_id}/image`}
            alt="AI 超分结果"
          />
        )}
        {mode !== "result" && (
          <div
            className="original-layer"
            style={
              mode === "compare"
                ? { clipPath: `inset(0 ${100 - split}% 0 0)` }
                : {}
            }
          >
            <img
              draggable="false"
              className="compare-image"
              style={imageStyle}
              src={source}
              alt="原始图片"
            />
          </div>
        )}
        {mode === "compare" && (
          <div className="split-handle" style={{ left: `${split}%` }}>
            <span>
              <MoveHorizontal size={17} />
            </span>
          </div>
        )}
        {mode !== "result" && <span className="tag before-tag">{['generate', 'base-upscale'].includes(job.workflow) ? '生成基础图' : '原图'}</span>}
        {mode !== "original" && <span className="tag after-tag">{job.processing_mode && job.processing_mode !== 'upscale' ? modeNames[job.processing_mode] : job.output_level === 'native' ? '基础图输出' : 'AI 超分'}</span>}
      </div>
      {mode === "compare" && (
        <input
          className="split-range"
          type="range"
          min="0"
          max="100"
          value={split}
          onChange={(e) => setSplit(Number(e.target.value))}
          aria-label="Before After 对比位置"
        />
      )}
    </>
  );
}

function App() {
  const [processingMode, setProcessingMode] = useState('upscale'),
    [shrinkScale, setShrinkScale] = useState('0.5');
  const [workflow, setWorkflow] = useState('upload'),
    [prompt, setPrompt] = useState(''),
    [aspect, setAspect] = useState('landscape'),
    [level, setLevel] = useState('4K'),
    [generation, setGeneration] = useState(null);
  const [file, setFile] = useState(null),
    [sourceUrl, setSourceUrl] = useState(""),
    [drag, setDrag] = useState(false);
  const [scale, setScale] = useState("2"),
    [resolution, setResolution] = useState("scale"),
    [model, setModel] = useState("RealESRGAN_x4plus");
  const [format, setFormat] = useState("png"),
    [quality, setQuality] = useState(95),
    [face, setFace] = useState(false);
  const [job, setJob] = useState(null),
    [error, setError] = useState("");
  const [engine, setEngine] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  const input = useRef(null),
    timer = useRef(null),
    pollingVersion = useRef(0),
    pollFailures = useRef(0);
  const output = useMemo(
    () => targetSize(workflow === 'generate' ? (job?.base_available ? {width: job.base_width, height: job.base_height} : {width: aspect === 'portrait' ? 1024 : aspect === 'landscape' ? 1536 : 1024, height: aspect === 'portrait' ? 1536 : 1024}) : file, Number(processingMode === 'upscale' ? scale : shrinkScale), processingMode !== 'upscale' ? 'scale' : workflow === 'generate' ? (level === 'native' ? 'original' : level) : resolution),
    [file, scale, resolution, workflow, aspect, level, processingMode, shrinkScale, job?.base_available, job?.base_width, job?.base_height],
  );
  const busy = Boolean(job && ACTIVE.includes(job.status));
  const [now, setNow] = useState(() => Date.now());
  const problem = workflow === "upload" ? outputProblem(file, output, processingMode, resolution) : "";
  // Elapsed time counts from when the worker picked the job up; queue time is not included.
  const elapsed = busy && job.started_at ? Math.max(0, now / 1000 - job.started_at) : busy && job.created_at ? Math.max(0, now / 1000 - job.created_at) : 0;
  // Tile throughput is steady, so extrapolate only after a few tiles to avoid wild early estimates.
  const tileStart = useRef(null);
  const processing = busy && job.status === "processing" && job.tiles_total > 0;
  // Remember the first observed tile count; reset when a new job starts or an OOM retry restarts tiles.
  if (processing && (tileStart.current?.id !== job.job_id || job.tiles_done < tileStart.current.done))
    tileStart.current = { id: job.job_id, at: now, done: job.tiles_done };
  let eta = null;
  if (processing && job.tiles_done < job.tiles_total) {
    const mark = tileStart.current, measured = job.tiles_done - mark.done;
    if (measured >= 2) eta = ((now - mark.at) / 1000 / measured) * (job.tiles_total - job.tiles_done);
  }
  // One clock tick per second only while a job runs, for elapsed time and the ETA.
  useEffect(() => {
    if (!busy) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [busy]);
  // CPU jobs take minutes; show progress in the browser tab so users can switch away.
  useEffect(() => {
    const base = "AI Image Upscaler";
    if (busy) document.title = job.status === "generating" ? `生成中 · ${base}` : `${job.progress || 0}% · ${base}`;
    else document.title = job?.status === "completed" ? `已完成 · ${base}` : base;
  }, [busy, job?.status, job?.progress]);
  // Upload jobs cannot be restored after a refresh (the source preview lives only in this tab).
  useEffect(() => {
    if (!busy || workflow !== "upload") return;
    const guard = (e) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [busy, workflow]);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/health", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Engine unavailable");
        return response.json();
      })
      .then(setEngine)
      .catch((error) => {
        if (error.name !== "AbortError") setEngine({ ok: false });
      });
    fetch('/api/generation/config', {signal: controller.signal})
      .then(response => { if (!response.ok) throw new Error(); return response.json(); }).then(setGeneration)
      .catch(error => { if (error.name !== 'AbortError') setGeneration({available: false, reason: '生成服务暂不可用'}); });
    return () => controller.abort();
  }, []);
  const pick = (blob) => {
    if (busy || !blob) return;
    if (blob.size > 50 * 1048576) return setError("文件超过 50 MB 上传限制。");
    if (!["image/jpeg", "image/png", "image/webp", "image/tiff"].includes(blob.type))
      return setError("请选择 JPG、PNG、WEBP 或 TIFF 图片。");
    const extension = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/tiff": "tif" }[blob.type];
    const name = blob.name && blob.name !== "image.png" ? blob.name : `粘贴的图片.${extension}`;
    const url = URL.createObjectURL(blob),
      image = new Image();
    image.onload = () => {
      setFile({
        blob,
        name,
        size: blob.size,
        width: image.naturalWidth,
        height: image.naturalHeight,
      });
      setSourceUrl(url);
      setJob(null);
      setError("");
    };
    image.onerror = async () => {
      URL.revokeObjectURL(url);
      try {
        const data = new FormData();
        data.append("file", blob);
        const response = await fetch("/api/inspect", {
          method: "POST",
          body: data,
        });
        const info = await readJson(response);
        if (!response.ok) throw new Error(translateError(info.detail));
        setFile({
          blob,
          name,
          size: blob.size,
          width: info.width,
          height: info.height,
        });
        setSourceUrl(info.preview);
        setJob(null);
        setError("");
      } catch (e) {
        setError(`图片无法读取：${translateError(e.message)}`);
      }
    };
    image.src = url;
  };
  // Paste from clipboard (screenshots, copied images) and block the browser from
  // navigating away when a file is dropped outside the drop zone.
  const pickRef = useRef(pick), workflowRef = useRef(workflow);
  pickRef.current = pick;
  workflowRef.current = workflow;
  useEffect(() => {
    const paste = (e) => {
      if (e.target.closest?.("textarea, input")) return;
      const item = [...(e.clipboardData?.items || [])].find((entry) => entry.kind === "file" && entry.type.startsWith("image/"));
      if (!item) return;
      if (workflowRef.current !== "upload") return;
      e.preventDefault();
      pickRef.current(item.getAsFile());
    };
    const block = (e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); };
    window.addEventListener("paste", paste);
    window.addEventListener("dragover", block);
    window.addEventListener("drop", block);
    return () => {
      window.removeEventListener("paste", paste);
      window.removeEventListener("dragover", block);
      window.removeEventListener("drop", block);
    };
  }, []);
  useEffect(
    () => () => {
      if (sourceUrl) URL.revokeObjectURL(sourceUrl);
    },
    [sourceUrl],
  );
  useEffect(() => () => { pollingVersion.current++; clearTimeout(timer.current); }, []);
  const pollJob = async (jobId, version, restoreSettings = false) => {
    try {
      const response = await fetch(`${API}/api/upscale/${jobId}/status`);
      const status = await readJson(response);
      if (version !== pollingVersion.current) return;
      if (!response.ok) {
        if ([404, 410].includes(response.status)) {
          localStorage.removeItem('upscaler-generated-job');
          setError('任务已过期，请创建新任务。');
          setJob(null);
          return;
        }
        throw new Error(status.detail || '读取失败');
      }
      setJob(status);
      if (['generate', 'base-upscale'].includes(status.workflow)) {
        if (restoreSettings) {
          if (status.aspect) setAspect(status.aspect);
          if (status.output_level) setLevel(status.output_level);
          if (status.model) setModel(status.model);
          if (status.processing_mode) setProcessingMode(status.processing_mode);
          if (status.shrink_scale) setShrinkScale(String(status.shrink_scale));
        }
        setSourceUrl(status.base_available ? `/api/upscale/${jobId}/base` : '');
        localStorage.setItem('upscaler-generated-job', jobId);
      }
      pollFailures.current = 0;
      setError('');
      if (!['completed', 'failed', 'cancelled'].includes(status.status))
        // Poll quickly while tiles move, slower while queued or while the tab is hidden.
        timer.current = setTimeout(() => pollJob(jobId, version), document.hidden ? 4000 : status.status === 'queued' ? 2000 : 900);
    } catch (e) {
      if (version !== pollingVersion.current) return;
      pollFailures.current += 1;
      setError(`无法读取任务状态：${translateError(e.message)}`);
      // Back off up to 15 s while the engine is unreachable instead of hammering it.
      timer.current = setTimeout(() => pollJob(jobId, version), Math.min(15000, 1500 * 2 ** (pollFailures.current - 1)));
    }
  };
  useEffect(() => {
    const saved = localStorage.getItem('upscaler-generated-job');
    if (saved && /^[a-f0-9]{32}$/.test(saved)) {
      setWorkflow('generate');
      setJob({job_id: saved, status: 'queued'});
      pollJob(saved, ++pollingVersion.current, true);
    }
  }, []);
  const start = async (reuseBase = false) => {
    if (busy || (!reuseBase && (workflow === 'upload' ? !file || problem : !prompt.trim() || !generation?.available))) return;
    const previousJob = job;
    const requestId = crypto.randomUUID();
    const version = ++pollingVersion.current;
    clearTimeout(timer.current);
    setError("");
    setCancelling(false);
    if (workflow === 'generate' && !reuseBase) setSourceUrl('');
    setJob({ status: "queued", progress: 0, current_stage: "Queued" });
    const data = new FormData();
    if (file) data.append("file", file.blob, file.name);
    data.append('processing_mode', processingMode);
    data.append("scale", processingMode === 'upscale' ? scale : shrinkScale);
    data.append("resolution", processingMode === 'upscale' ? resolution : 'scale');
    data.append("model", model);
    data.append("face_enhance", workflow === 'upload' && processingMode === 'upscale' && face);
    data.append("output_format", format);
    const safeQuality = Math.max(1, Math.min(100, Math.round(Number(quality)) || 95));
    data.append("quality", safeQuality);
    try {
      const res = await fetch(`${API}${reuseBase ? `/api/upscale/${previousJob.job_id}/continue` : workflow === 'generate' ? '/api/generate' : '/api/upscale'}`, {
        method: "POST",
        ...(workflow === 'generate' ? {headers: {'Content-Type': 'application/json', 'Idempotency-Key': requestId}, body: JSON.stringify({...(reuseBase ? {} : {prompt, aspect}), processing_mode: processingMode, shrink_scale: Number(shrinkScale), output_level: level === 'native' && reuseBase ? '4K' : level, model, output_format: format, quality: safeQuality})} : {headers: {'Idempotency-Key': requestId}, body: data}),
      });
      const payload = await readJson(res);
      if (!res.ok) throw new Error(Array.isArray(payload.detail) ? payload.detail.map(item => item.msg).join('; ') : payload.detail || "提交失败");
      setJob(payload);
      if (workflow === 'generate') localStorage.setItem('upscaler-generated-job', payload.job_id);
      pollJob(payload.job_id, version);
    } catch (e) {
      setJob(reuseBase ? previousJob : null);
      setError(translateError(e.message));
    }
  };
  // Ctrl/⌘ + Enter starts the main action from anywhere, including the prompt box.
  const startRef = useRef(start);
  startRef.current = start;
  useEffect(() => {
    const shortcut = (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); startRef.current(false); }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);
  const cancel = async () => {
    if (!job?.job_id || cancelling) return;
    setCancelling(true);
    try {
      const response = await fetch(`${API}/api/upscale/${job.job_id}/cancel`, {
        method: "POST",
      });
      if (!response.ok) throw new Error("取消请求失败，请重试。");
    } catch (error) {
      setError(error.message);
      setCancelling(false);
    }
  };
  const reset = () => {
    if (busy) return;
    pollingVersion.current++;
    clearTimeout(timer.current);
    localStorage.removeItem('upscaler-generated-job');
    setFile(null);
    setSourceUrl("");
    setJob(null);
    setError("");
    input.current.value = "";
  };
  const changeWorkflow = (value) => {
    if (busy) return;
    reset();
    setWorkflow(value);
  };
  const completed = job?.status === "completed";
  const loadSample = async (e) => {
    e.stopPropagation();
    try {
      const response = await fetch("/api/sample");
      if (!response.ok) throw new Error("示例照片暂不可用");
      pick(
        new File([await response.blob()], "示例照片.jpg", {
          type: "image/jpeg",
        }),
      );
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <div className="app-shell">
      <aside className="navigation-rail" aria-label="工作台导航">
        <a className="app-mark" href="#workspace" title="AI Image Upscaler" aria-label="AI Image Upscaler"><Sparkles size={23} /></a>
        <nav>
          <a className="rail-button active" href="#workspace" title="图片工作区" aria-label="图片工作区"><ImageIcon size={20} /></a>
          <a className="rail-button" href="#output-settings" title="输出设置" aria-label="输出设置"><SlidersHorizontal size={20} /></a>
          <button className="rail-button" disabled={!completed} title="查看结果" aria-label="查看结果" onClick={() => document.getElementById("result")?.scrollIntoView({ behavior: "smooth", block: "center" })}><Layers size={20} /></button>
        </nav>
        <span className="rail-bottom">NL</span>
      </aside>
      <div className="app-content">
      <header className="topbar">
        <div className="brand">
          <b>AI Image Upscaler</b>
          <span className="brand-divider" /><span className="edition">Night Lab</span>
        </div>
        <span className={`status ${engine?.ok ? "online" : ""}`}>
          <i /> {engine?.ok ? `本地引擎 · ${engine.device.toUpperCase()}` : engine ? "引擎离线" : "连接中"}
        </span>
      </header>
      <main id="workspace">
        <section className="intro">
          <div><p className="eyebrow">WORKSPACE</p><h1>AI 图片超分</h1></div>
          <span className="intro-meta"><span className="tiny-dot" /> Real-ESRGAN <span className="meta-divider">/</span> 最高 8K</span>
        </section>
        <div className="workflow-switch segments" role="group" aria-label="工作模式">
          <button disabled={busy} className={workflow === 'upload' ? 'active' : ''} aria-pressed={workflow === 'upload'} onClick={() => changeWorkflow('upload')}><Upload size={14} /> 上传超分</button>
          <button disabled={busy} className={workflow === 'generate' ? 'active' : ''} aria-pressed={workflow === 'generate'} onClick={() => changeWorkflow('generate')}><Sparkles size={14} /> AI 生成</button>
        </div>
        <section className="workspace">
          <div className="stage">
            <div className="stage-heading">
              <div className="stage-title"><ImageIcon size={17} /><span>{workflow === 'generate' ? 'gpt-image-2 · 生成工作区' : file?.name || "图片工作区"}</span></div>
              <div className="stage-actions">
                <span className={`stage-badge ${completed ? "complete" : busy ? "running" : job?.status === "failed" ? "failed" : ""}`}>{completed ? <><Check size={12} /> 已完成</> : busy ? "处理中" : job?.status === "failed" ? "失败" : job?.status === "cancelled" ? "已取消" : file ? "待处理" : "等待导入"}</span>
                {file && <button className="icon-button" title="更换图片" aria-label="更换图片" disabled={busy} onClick={reset}><X size={16} /></button>}
              </div>
            </div>
            {completed ? (
              <div className="result" id="result">
                <Comparator source={sourceUrl} job={job} />
                <div className="result-meta">
                  <span>{job.input_width} × {job.input_height} <ArrowUpRight size={13} /> <b>{job.output_width} × {job.output_height}</b>{job.input_width ? <em>{(job.output_width / job.input_width).toFixed(2).replace(/\.?0+$/, '')}×</em> : null}</span>
                  <span>耗时 {(job.processing_ms || 0) < 1000 ? '不到 1 秒' : formatDuration(job.processing_ms / 1000)} · {formatBytes(job.output_size)}</span>
                </div>
              </div>
            ) : workflow === 'generate' ? (
              <div className="generation-editor">
                {sourceUrl && job?.base_width ? <img className="generated-preview" src={sourceUrl} alt="生成基础图" /> : <div className="generation-heading"><Sparkles size={23} /><h2>AI 图像创作</h2></div>}
                <label htmlFor="generation-prompt">画面描述</label>
                <textarea id="generation-prompt" aria-label="画面描述" maxLength={16000} value={prompt} disabled={busy} onChange={e => setPrompt(e.target.value)} placeholder="例如：一辆银色复古自行车，停在有绿植的白色建筑前，清晨自然光，细节清晰。" />
                <div className="generation-options"><label htmlFor="aspect-select">画面比例<select id="aspect-select" aria-label="画面比例" value={aspect} disabled={busy} onChange={e => setAspect(e.target.value)}><option value="square">方形 · 1:1</option><option value="landscape">横向 · 3:2</option><option value="portrait">竖向 · 2:3</option></select></label><span>{job?.base_available ? `${job.base_width} × ${job.base_height}` : aspect === 'square' ? '1024 × 1024' : aspect === 'landscape' ? '1536 × 1024' : '1024 × 1536'} 基础图</span></div>
                <p className="generation-disclosure">提示词发送至 Tuzi · 生成请求可能消耗 API 额度</p>
                {generation && !generation.available && <div className="error" role="alert">{generation.reason}</div>}
              </div>
            ) : (
            <div
              className={`dropzone ${drag ? "drag" : ""} ${file ? "has-file" : ""}`}
              onClick={() => !file && input.current.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDrag(true);
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDrag(false);
                pick(e.dataTransfer.files[0]);
              }}
            >
              {file ? (
                <>
                  <img className="source-preview" src={sourceUrl} alt={file.name} />
                  <div className="source-info">
                    <b>{file.name}</b>
                    <span>
                      {file.width} × {file.height} ·{" "}
                      {(file.size / 1048576).toFixed(1)} MB
                    </span>
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation();
                        reset();
                      }}
                    >
                      <X size={14} /> 更换图片
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div className="upload-icon">
                    <Upload size={25} />
                  </div>
                  <h2>{drag ? "松开即可导入" : "从一张图片开始"}</h2>
                  <p>拖入、点击选择，或按 <kbd>Ctrl</kbd> + <kbd>V</kbd> 粘贴截图</p>
                  <p className="dropzone-limits">JPG、PNG、WEBP、TIFF，最大 50 MB / 33 MP</p>
                  <button className="import-button" onClick={(event) => { event.stopPropagation(); input.current.click(); }}><Plus size={16} /> 选择图片</button>
                  <button className="sample-button" onClick={loadSample}>
                    <img src="/api/sample" alt="示例照片" />
                    <span>示例照片 <ArrowUpRight size={14} /></span>
                  </button>
                </>
              )}
            </div>
            )}
              <input
                ref={input}
                className="file-input"
                type="file"
                accept="image/jpeg,image/png,image/webp,image/tiff"
                onChange={(e) => { pick(e.target.files[0]); e.target.value = ""; }}
              />
            <div className="stage-footer"><span><span className="tiny-dot" /> {completed ? "处理完成" : job?.status === 'generating' ? '正在生成基础图' : busy ? "AI 正在处理" : workflow === 'generate' ? 'gpt-image-2 → Real-ESRGAN' : "文件仅在本机处理"}</span><span>保留原始宽高比</span></div>
          </div>
          <aside className="controls" id="output-settings">
            <fieldset className="settings" disabled={busy}>
              <div className="section-head">
                <div>
                  <p className="eyebrow">INSPECTOR</p>
                  <h2>输出设置</h2>
                </div>
                <SlidersHorizontal size={18} className="inspector-icon" />
              </div>
              <label htmlFor="processing-mode">处理方式<select id="processing-mode" aria-label="处理方式" value={processingMode} onChange={e => setProcessingMode(e.target.value)}>{Object.entries(modeNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              {processingMode !== 'upscale' && <label htmlFor="shrink-scale">缩小比例<select id="shrink-scale" aria-label="缩小比例" value={shrinkScale} onChange={e => setShrinkScale(e.target.value)}><option value="0.5">50% · 宽高减半</option><option value="0.25">25% · 宽高四分之一</option></select></label>}
              <p className="mode-note">{processingMode === 'resize' ? '保留原有内容，适合文字、Logo 和已经清晰的图片。' : processingMode === 'ai_resize' ? 'AI 增强后缩小，可能重构文字和人脸；耗时接近 AI 超分。' : 'AI 可能重构细节，下载前建议查看原图对比。'}</p>
              <label htmlFor="model-select" hidden={processingMode === 'resize'}>
                AI 模型
                <select
                  id="model-select"
                  aria-label="AI 模型"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                >
                  <option value="RealESRGAN_x4plus">
                    RealESRGAN x4plus · 照片
                  </option>
                  <option value="RealESRGAN_x4plus_anime_6B">
                    RealESRGAN x4plus anime · 插画
                  </option>
                </select>
              </label>
              {processingMode === 'upscale' && (workflow === 'generate' ? <div className="generation-levels"><span className="field-label">输出分辨率</span><div className="scale-segments" role="group" aria-label="生成输出分辨率">{[['native', '基础图'], ['2K', '超分 2K'], ['4K', '超分 4K']].map(([value, label]) => <button key={value} className={level === value ? 'active' : ''} aria-pressed={level === value} onClick={() => setLevel(value)}>{label}</button>)}</div></div> : <div className="two-col">
                <div className="setting-field"><span className="field-label" id="scale-label">放大倍率</span><div className="scale-segments" role="group" aria-labelledby="scale-label">{["1", "2", "4"].map((value) => <button type="button" key={value} className={scale === value ? "active" : ""} aria-pressed={scale === value} onClick={() => setScale(value)}>{value}×</button>)}</div></div>
                <label htmlFor="resolution-select">
                  目标分辨率
                  <select
                    id="resolution-select"
                    aria-label="目标分辨率"
                    value={resolution}
                    onChange={(e) => setResolution(e.target.value)}
                  >
                    <option value="scale">按倍率</option>
                    <option value="original">原尺寸增强</option>
                    <option value="2K">2K</option>
                    <option value="4K">4K</option>
                    <option value="8K">8K</option>
                  </select>
                </label>
              </div>)}
              <div className="estimate">
                {output ? (
                  <>
                    预计输出{" "}
                    <strong>
                      {output[0].toLocaleString()} ×{" "}
                      {output[1].toLocaleString()}
                    </strong>{" "}
                    · {((output[0] * output[1]) / 1e6).toFixed(1)} MP
                  </>
                ) : (
                  "等待上传图片"
                )}
                {problem && <p className="estimate-warning" role="alert"><TriangleAlert size={13} /> {problem}</p>}
              </div>
              <div className="two-col">
                <label htmlFor="format-select">
                  输出格式
                  <select
                    id="format-select"
                    aria-label="输出格式"
                    value={format}
                    onChange={(e) => setFormat(e.target.value)}
                  >
                    <option value="png">PNG · 无损</option>
                    <option value="jpg">JPG</option>
                    <option value="webp">WEBP</option>
                  </select>
                </label>
                <label htmlFor="quality-input">
                  质量
                  <input
                    id="quality-input"
                    aria-label="质量"
                    type="number"
                    min="1"
                    max="100"
                    value={quality}
                    disabled={format === "png"}
                    onChange={(e) => setQuality(e.target.value)}
                    onBlur={() => setQuality((value) => Math.max(1, Math.min(100, Math.round(Number(value)) || 95)))}
                  />
                </label>
              </div>
              {workflow === 'upload' && processingMode === 'upscale' && <label className="toggle">
                <input
                  type="checkbox"
                  role="switch"
                  aria-label="人脸增强"
                  checked={face}
                  onChange={(e) => setFace(e.target.checked)}
                />
                <span className="switch" aria-hidden="true" />
                <span>
                  人脸增强 <small>人脸增强可能重新构建部分面部细节。</small>
                </span>
              </label>}
            </fieldset>
            {completed && <a className="download" href={`/api/upscale/${job.job_id}/download`} download><Download size={17} />{job.processing_mode && job.processing_mode !== 'upscale' ? '下载缩图结果' : job.output_level === 'native' ? '下载基础图' : Math.max(job.output_width, job.output_height) === 7680 ? "下载 8K 图片" : "下载超分图片"}</a>}
            {workflow === 'generate' && job?.base_available && <button className="continue-button" disabled={busy} onClick={() => start(true)}><Layers size={17} />{processingMode !== 'upscale' ? `基础图${modeNames[processingMode]}` : level === 'native' ? '基础图超分至 4K' : `基础图超分至 ${level}`}</button>}
            <button
              className="primary"
              disabled={busy || (workflow === 'upload' ? !file || Boolean(problem) : !prompt.trim() || !generation?.available)}
              onClick={() => start(false)}
              title="Ctrl + Enter"
            >
              <Sparkles size={17} /> {busy ? "正在处理" : processingMode !== 'upscale' ? `${workflow === 'generate' ? '生成并' : '开始'}${modeNames[processingMode]}` : workflow === 'generate' ? completed ? '再次生成' : level === 'native' ? '生成基础图' : `生成并超分 ${level}` : completed ? "重新处理" : "开始 AI 超分"}
            </button>
            {busy && (
              <div className="progress-wrap" role="status" aria-live="polite">
                <div className="progress-label">
                  <span>{stageLabel(job)}</span>
                  <strong>{job.status === 'generating' ? '等待返回' : `${job.progress || 0}%`}</strong>
                </div>
                <div className={`progress ${['generating', 'queued'].includes(job.status) || (job.status === 'loading_model') ? 'indeterminate' : ''}`}>
                  <i style={{ width: `${job.progress || 0}%` }} />
                </div>
                {job.status === 'processing' && <TileMap done={job.tiles_done} total={job.tiles_total} width={job.input_width} height={job.input_height} />}
                <div className="progress-meta">
                  <span><Clock size={12} /> 已用 {formatDuration(elapsed) || '0 秒'}</span>
                  {eta != null && <span>预计还需 {formatDuration(eta)}</span>}
                </div>
                <button className="text-button" disabled={!job.job_id || cancelling} onClick={cancel}>
                  <X size={13} /> {cancelling ? "正在取消" : "取消任务"}
                </button>
              </div>
            )}
            {(error || job?.error) && (
              <div className={job?.status === 'cancelled' && !error ? 'notice' : 'error'} role="alert">
                {job?.status !== 'cancelled' && <TriangleAlert size={15} />}
                <span>{error || translateError(job.error)}</span>
              </div>
            )}
            <div className="inspector-footer"><span>{model === "RealESRGAN_x4plus" ? "照片模型" : "插画模型"}</span><span>输出有效期 24 小时</span></div>
          </aside>
        </section>
        <footer><span>Night Lab <span className="meta-divider">/</span> AI Image Upscaler</span><span>{completed ? job.processing_mode === 'resize' ? 'Lanczos · 保真缩图' : job.output_level === 'native' && job.processing_mode !== 'ai_resize' ? 'gpt-image-2 · 基础图' : `${job.device.toUpperCase()} · Tile ${job.tile_size}px` : "本地工作台"}</span></footer>
      </main>
      </div>
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
