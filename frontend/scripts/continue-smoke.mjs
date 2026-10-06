import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
const jobId = process.argv[2];
assert(/^[a-f0-9]{32}$/.test(jobId || ''), 'Pass a persisted job containing a reusable base image.');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= root + '.cache/playwright';
const {chromium} = await import('playwright');
const output = root + 'data/validation/continue';
await mkdir(output, {recursive: true});
const browser = await chromium.launch({headless: true});
try {
  const context = await browser.newContext({viewport: {width: 1366, height: 900}, acceptDownloads: true});
  await context.addInitScript(id => {
    if (!localStorage.getItem('upscaler-generated-job')) localStorage.setItem('upscaler-generated-job', id);
  }, jobId);
  const page = await context.newPage();
  const errors = [];
  const paid = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/generate', route => { paid.push(route.request().url()); return route.abort(); });
  await page.goto('http://127.0.0.1:8000/', {waitUntil: 'networkidle'});
  await page.waitForSelector('.continue-button');
  await page.getByLabel('AI 模型', {exact: true}).selectOption('RealESRGAN_x4plus_anime_6B');
  await page.getByRole('button', {name: '超分 2K', exact: true}).click();
  const submissionPromise = page.waitForResponse(r => r.url().endsWith('/continue') && r.request().method() === 'POST');
  await page.locator('.continue-button').click();
  const submission = await submissionPromise;
  assert(submission.ok(), await submission.text());
  const submitted = await submission.json();
  console.log('Real SR continuation submitted:', submitted.job_id);
  await page.reload({waitUntil: 'networkidle'});
  assert.equal(await page.evaluate(() => localStorage.getItem('upscaler-generated-job')), submitted.job_id);
  const deadline = Date.now() + 1800000;
  let status;
  while (Date.now() < deadline) {
    const response = await page.request.get(`http://127.0.0.1:8000/api/upscale/${submitted.job_id}/status`);
    assert(response.ok());
    status = await response.json();
    if (['completed', 'failed', 'cancelled'].includes(status.status)) break;
    console.log(status.status, status.tiles_done, '/', status.tiles_total);
    await new Promise(resolve => setTimeout(resolve, 10000));
  }
  assert.equal(status.status, 'completed', status.error);
  assert.equal(status.workflow, 'base-upscale');
  assert.equal(status.base_available, true);
  await page.waitForSelector('.compare-image');
  await page.waitForFunction(() => [...document.querySelectorAll('.compare-image')].every(i => i.complete && i.naturalWidth > 0));
  await page.screenshot({path: output + '/1366-result.png', fullPage: true});
  const downloadPromise = page.waitForEvent('download');
  await page.locator('.download').click();
  await (await downloadPromise).saveAs(output + '/result-2k.png');
  await page.setViewportSize({width: 390, height: 844});
  await page.reload({waitUntil: 'networkidle'});
  await page.waitForSelector('.compare-image');
  await page.waitForFunction(() => [...document.querySelectorAll('.compare-image')].every(i => i.complete && i.naturalWidth > 0));
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path: output + '/390-result.png', fullPage: true});
  assert.deepEqual(paid, []);
  assert.deepEqual(errors, []);
  await writeFile(output + '/report.json', JSON.stringify({...status, source_job_id: jobId, paid_generation_requests: paid.length, refresh_restoration: true, browser_errors: errors}, null, 2));
  console.log('PASS: real continuation, reload recovery, desktop/mobile comparison and download; zero paid generation calls.');
} finally {
  await browser.close();
}
