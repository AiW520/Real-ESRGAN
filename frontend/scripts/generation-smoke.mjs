import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH ||= root + '.cache/playwright';
const {chromium} = await import('playwright');
const output = root + 'data/validation/generation';
const localJob = process.env.GENERATION_LOCAL_JOB;
if (!localJob && process.env.TUZI_LIVE_VERIFY !== '1') throw new Error('Paid verification must be explicitly enabled with TUZI_LIVE_VERIFY=1; no automatic retries.');
await mkdir(output, {recursive: true});
const browser = await chromium.launch({headless: true});
const errors = [];
try {
  for (const width of [1366, 390]) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:8000/', {waitUntil: 'networkidle'});
    await page.getByRole('button', {name: 'AI 生成', exact: true}).click();
    await page.getByLabel('画面描述', {exact: true}).fill('一辆银色复古自行车，停在有绿植的白色建筑前，清晨自然光，细节清晰。');
    await page.getByLabel('画面比例', {exact: true}).selectOption('square');
    await page.getByRole('button', {name: '超分 2K', exact: true}).click();
    assert((await page.locator('.estimate').innerText()).includes('1,920'));
    await page.getByLabel('画面比例', {exact: true}).selectOption('portrait');
    await page.getByRole('button', {name: '超分 4K', exact: true}).click();
    assert(await page.locator('.primary').isEnabled());
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: `${output}/${width}-editor.png`, fullPage: true});
    await page.close();
  }
  const page = await browser.newPage({viewport: {width: 1366, height: 900}, acceptDownloads: true});
  if (localJob) await page.route('**/api/generate', route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({job_id: localJob, status: 'queued'})}));
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:8000/', {waitUntil: 'networkidle'});
  await page.getByRole('button', {name: 'AI 生成', exact: true}).click();
  await page.getByLabel('画面描述', {exact: true}).fill('A detailed clean hand-painted illustration of a silver vintage bicycle parked beside a white greenhouse filled with lush green plants, warm morning sunlight, crisp spokes and leaves, small red flowers, natural colors, full bicycle visible, landscape composition, no text, no logo.');
  await page.getByLabel('AI 模型', {exact: true}).selectOption('RealESRGAN_x4plus_anime_6B');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/generate') && response.request().method() === 'POST');
  await page.locator('.primary').click();
  const submission = await responsePromise;
  assert(submission.ok(), await submission.text());
  const job = await submission.json();
  await writeFile(`${output}/job.json`, JSON.stringify(job, null, 2));
  console.log(localJob ? 'Replaying LOCAL FIXTURE job for browser QA:' : 'Submitted ONE real generation + SR job:', job.job_id);
  const started = Date.now();
  let status;
  while (Date.now() - started < 1800000) {
    const response = await page.request.get(`http://127.0.0.1:8000/api/upscale/${job.job_id}/status`);
    status = await response.json();
    if (['completed', 'failed', 'cancelled'].includes(status.status)) break;
    console.log(status.status, status.progress, status.tiles_done, '/', status.tiles_total);
    await new Promise(resolve => setTimeout(resolve, 10000));
  }
  await writeFile(`${output}/status.json`, JSON.stringify(status, null, 2));
  assert.equal(status.status, 'completed', status.error);
  assert(status.base_width > 0 && status.base_height > 0);
  assert(status.output_width > status.base_width);
  await page.waitForSelector('.compare-image');
  await page.waitForFunction(() => [...document.querySelectorAll('.compare-image')].every(image => image.complete && image.naturalWidth > 0));
  await page.screenshot({path: `${output}/1366-result.png`, fullPage: true});
  await page.getByRole('button', {name: '100% 像素查看', exact: true}).click();
  await page.screenshot({path: `${output}/1366-detail.png`, fullPage: true});
  const downloadPromise = page.waitForEvent('download');
  await page.locator('.download').click();
  const download = await downloadPromise;
  await download.saveAs(`${output}/result-4k.png`);
  const base = await page.request.get(`http://127.0.0.1:8000/api/upscale/${job.job_id}/base`);
  assert(base.ok());
  await writeFile(`${output}/base.png`, await base.body());
  await page.setViewportSize({width: 390, height: 844});
  await page.getByTitle('适应窗口', {exact: true}).click();
  await page.screenshot({path: `${output}/390-result.png`, fullPage: true});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.deepEqual(errors, []);
  console.log(localJob ? 'PASS: local base replay, real SR output, dimensions, download, comparison, desktop/mobile; generation API not tested.' : 'PASS: real generation, real SR, dimensions, download, comparison, desktop/mobile.');
} finally {
  await browser.close();
}
