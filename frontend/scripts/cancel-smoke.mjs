import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {mkdir, writeFile} from 'node:fs/promises';
const root = fileURLToPath(new URL('../../', import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH ||= root + '.cache/playwright';
const {chromium} = await import('playwright');
const output = root + 'data/validation/cancel';
await mkdir(output, {recursive:true});
const browser = await chromium.launch({headless:true});
const page = await browser.newPage();
const errors = [], states = [];
page.on('pageerror', e => errors.push(e.message));
page.on('response', async r => {
  if (r.url().endsWith('/status')) states.push(await r.json().catch(() => ({invalid:true})));
});
try {
  await page.goto('http://127.0.0.1:8000/');
  await page.locator('input[type=file]').setInputFiles(root + 'data/fixtures/photo.jpg');
  await page.waitForSelector('.source-preview');
  await page.locator('.primary').click();
  await page.getByRole('button', {name:'取消任务',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('.primary')?.disabled === false, null, {timeout:120000});
  assert.equal(await page.locator('.download').count(), 0);
  assert(states.some(s => s.status === 'cancelled'));
  assert.deepEqual(errors, []);
  console.log('Cancellation restores a usable UI: passed');
} finally {
  await page.screenshot({path:output + '/page.png',fullPage:true}).catch(() => {});
  await writeFile(output + '/report.json', JSON.stringify({errors,states,body:await page.locator('body').innerText()},null,2));
  await browser.close();
}
