import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH ||= root + '.cache/playwright';
const {chromium} = await import('playwright');
const output = root + 'data/validation/quality-audit';
await mkdir(output, {recursive: true});
const browser = await chromium.launch({headless: true});
const reports = [], errors = [], paidCalls = [];
try {
  for (const [mode, width] of [['resize', 1366], ['ai_resize', 390]]) {
    const page = await browser.newPage({viewport: {width, height: 900}, acceptDownloads: true});
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/api/generate', route => {paidCalls.push(route.request().url()); return route.abort();});
    await page.goto('http://127.0.0.1:8000/', {waitUntil:'networkidle'});
    await page.locator('input[type=file]').setInputFiles(output + '/photo-input.png');
    await page.waitForSelector('.source-preview');
    await page.getByLabel('处理方式', {exact:true}).selectOption(mode);
    await page.getByLabel('缩小比例', {exact:true}).selectOption('0.5');
    assert((await page.locator('.estimate').innerText()).includes('64 × 64'));
    assert.equal(await page.getByLabel('AI 模型', {exact:true}).isVisible(), mode === 'ai_resize');
    const submissionPromise = page.waitForResponse(r => r.url().endsWith('/api/upscale') && r.request().method() === 'POST');
    await page.locator('.primary').click();
    const submission = await submissionPromise;
    assert(submission.ok(), await submission.text());
    const {job_id} = await submission.json();
    await page.waitForSelector('.compare-image', {timeout:180000});
    await page.waitForFunction(() => [...document.querySelectorAll('.compare-image')].every(i => i.complete && i.naturalWidth > 0));
    const status = await (await page.request.get(`http://127.0.0.1:8000/api/upscale/${job_id}/status`)).json();
    assert.equal(status.status, 'completed');
    assert.equal(status.processing_mode, mode);
    assert.equal(status.output_width, 64);
    assert.equal(status.output_height, 64);
    assert.equal(status.tiles_total > 0, mode === 'ai_resize');
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('link', {name:'下载缩图结果'}).click();
    await (await downloadPromise).saveAs(`${output}/${mode}-api.png`);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path:`${output}/${mode}-ui.png`, fullPage:true});
    reports.push(status);
    await page.close();
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(paidCalls, []);
  await writeFile(`${output}/resize-browser.json`, JSON.stringify({reports, errors, paidCalls}, null, 2));
  console.log('PASS: real plain + AI shrink API, correct dimensions, model bypass/use, UI preview and downloads, zero paid generation calls.');
} finally {
  await browser.close();
}
