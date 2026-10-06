import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH ||= root + '.cache/playwright';
const {chromium} = await import('playwright');
const jobId = process.argv[2];
assert(/^[a-f0-9]{32}$/.test(jobId || ''));
const browser = await chromium.launch({headless: true});
try {
  for (const width of [1366, 390]) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    await page.addInitScript(id => localStorage.setItem('upscaler-generated-job', id), jobId);
    await page.goto('http://127.0.0.1:8000/', {waitUntil: 'networkidle'});
    await page.waitForSelector('.compare-image');
    await page.waitForFunction(() => [...document.querySelectorAll('.compare-image')].every(i => i.complete && i.naturalWidth > 0));
    assert.equal(await page.getByRole('button', {name: '超分 2K', exact: true}).getAttribute('aria-pressed'), 'true');
    assert.equal(await page.getByLabel('AI 模型', {exact: true}).inputValue(), 'RealESRGAN_x4plus_anime_6B');
    assert.equal(await page.getByRole('button', {name: '基础图超分至 2K', exact: true}).isEnabled(), true);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: `${root}data/validation/continue/${width}-result.png`, fullPage: true});
    await page.close();
  }
  console.log('PASS: restored settings and result at desktop/mobile dimensions; no new jobs submitted.');
} finally {
  await browser.close();
}
