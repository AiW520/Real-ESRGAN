"""Build a self-contained visual report from real audit artifacts."""
import base64
import html
import io
import json
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
out = root / 'data/validation/quality-audit'
data = json.loads((out / 'report.json').read_text('utf-8'))
client = json.loads((out / 'platform-client-report.json').read_text('utf-8'))
resize = json.loads((out / 'resize-browser.json').read_text('utf-8'))
detail = Image.new('RGB', (1000, 540), '#17191e')
draw = ImageDraw.Draw(detail)
for index, (filename, label) in enumerate([('photo-4k-lanczos.png', 'Lanczos - 100% crop'), ('photo-4k-ai.png', 'Real-ESRGAN - 100% crop')]):
    draw.text((index * 500 + 12, 12), label, fill='white')
    with Image.open(out / filename) as source:
        detail.paste(source.crop((2050, 1250, 2550, 1750)), (index * 500, 40))
detail.save(out / 'photo-4k-detail.png')


def image(filename):
    with Image.open(out / filename) as source:
        picture = source.convert('RGB')
        picture.thumbnail((1440, 1000), Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        picture.save(stream, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')


rows = ''.join(f'<tr><td>{html.escape(item["name"])}</td><td>{item["input"][0]} × {item["input"][1]}</td>'
               f'<td>{item["output"][0]} × {item["output"][1]}</td><td>{item["milliseconds"]/1000:.1f} 秒</td>'
               f'<td>{item["tiles"]}</td></tr>' for item in data['cases'])
findings = [
    ('照片：更锐利，但细节重构', '左：清晰参考图；中：普通 Lanczos；右：Real-ESRGAN。人脸、徽章与细纹发生重构，不能用锐度代替真实性。照片 SSIM 从 0.718 升至 0.732，但 PSNR 从 23.82 降至 22.12 dB。', 'photo-comparison.png'),
    ('文字：清楚不等于字形一致', '本样本 SSIM 从 0.908 升至 0.969，但细看可见字体笔画改变。小字、Logo 和精确排版需要人工对照，不能保证自动保真。', 'text-comparison.png'),
    ('分块：检查接缝与局部差异', '左：单块；中：64px 分块；右：差异放大 8 倍。本图无明显拼接断裂，平均差异 0.253/255，99% 通道误差不超过 3，最大误差 30。不能推断所有图片都完全无接缝。', 'seams.png'),
    ('缩图：两种模式各有用途', '左：参考图缩至 64px；中：普通缩图；右：AI 超分后缩图。为方便检查，此处将 64px 图放大显示。本样本 SSIM 0.9804 → 0.9811，收益很小；普通缩图 PSNR 更高，且不重构语义细节。', 'downsample-comparison.png'),
    ('完整 4K：真实照片模型输出', '960×540 → 3840×2160，RealESRGAN_x4plus、CPU、12 个 tile、约 381 秒。该样本用于验证完整 4K 路径；不将其视为经过证实的原生 4K 真值。', 'photo-4k-comparison.png'),
    ('4K 局部：相同位置的 100% 裁切', '左：Lanczos；右：Real-ESRGAN。两块都是最终输出图中相同位置的 500×500 像素区域；未额外锐化。可见 AI 对反光和纹理做了平滑与重构。', 'photo-4k-detail.png'),
]
cards = ''.join(f'<section><h2>{title}</h2><p>{text}</p><img src="{image(file)}" alt="{html.escape(title)}"></section>' for title, text, file in findings)
shrinks = ''.join(f'<li>{"保真缩图" if row["processing_mode"] == "resize" else "AI 增强缩图"}：'
                  f'{row["input_width"]}×{row["input_height"]} → {row["output_width"]}×{row["output_height"]}，'
                  f'{row["processing_ms"]/1000:.2f} 秒，模型 tile 数 {row["tiles_total"]}。</li>' for row in resize['reports'])
document = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>真实超分质量测试 · Night Lab</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#141619;color:#ebedf1;font:15px/1.8 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1180px;margin:auto;padding:40px 24px}}h1{{font-size:34px;line-height:1.3}}h2{{font-size:22px}}p,li{{color:#b9c2cf}}a{{color:#a4dabe}}section{{margin:24px 0;padding:28px;border:1px solid #373e48;border-radius:24px;background:#1c2026}}img{{display:block;width:100%;height:auto;border-radius:14px}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #363d47}}.scroll{{overflow-x:auto}}.tag{{color:#a4dabe}}.warning{{border-left:3px solid #e8b86a;padding-left:16px}}@media(max-width:600px){{main{{padding:24px 14px}}h1{{font-size:27px}}section{{padding:18px}}}}
</style><main><a href="/">← 返回工作台</a><p class="tag">NIGHT LAB / REAL MODEL VALIDATION</p><h1>真实超分测试：结果与边界</h1>
<p>2026-10-04 · 本机 CPU · 官方 RealESRGAN_x4plus 权重 · HTTP 提交、真实推理、实际下载。</p>
<p class="warning">可以真实超分和缩图，但本轮观察到人脸、字形和徽章细节重构，不能承诺“完美还原”。没有调用生成 API，Tuzi 上游 502 是否恢复仍未验证，也未完成目标生成平台的代码接入。</p>
<section><h2>实际运行记录</h2><div class="scroll"><table><thead><tr><th>样本</th><th>输入</th><th>输出</th><th>耗时</th><th>Tile</th></tr></thead><tbody>{rows}</tbody></table></div><p>照片测试由清晰图缩小并做 JPEG 压缩后重建；文字测试由清晰文字图缩小后重建。透明通道与预期 Lanczos Alpha 逐像素一致。PSNR/SSIM 只反映本组参考图的误差，不能单独代表主观画质。</p></section>
{cards}<section><h2>缩图与平台调用验证</h2><ul>{shrinks}</ul><p>桌面/手机页面预览与下载通过。AI 缩图接口与完整超分后缩图的像素平均差异：{client['modes']['ai_resize']['mae_vs_full_sr_then_downsample']:.1f}。平台适配器完成提交、轮询和下载，25% 输出为 32×32。此次生成接口调用为 0。</p><p>保真缩图不运行模型；AI 增强缩图仍需神经网络推理。已有底图可通过 /api/upscale 提交，也可通过 /continue 复用本服务保存的生成底图。</p></section>
<p>所有源图、输出、JSON 报告和测试脚本保存在 D:\\图片超分。此报告内嵌图片，可离线保存。</p></main></html>'''
(out / 'index.html').write_text(document, encoding='utf-8')
public = root / 'frontend/public'
public.mkdir(exist_ok=True)
(public / 'quality-report.html').write_text(document, encoding='utf-8')
print(out / 'index.html')
