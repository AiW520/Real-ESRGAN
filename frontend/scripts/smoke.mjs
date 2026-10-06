import { fileURLToPath } from "node:url";
import { mkdir, writeFile } from "node:fs/promises";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH ||= fileURLToPath(
  new URL("../../.cache/playwright/", import.meta.url),
);
const { chromium } = await import("playwright");
const output = root + "data/validation/browser/";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const errors = [],
  report = [];
let activePage;
try {
  for (const viewport of [
    { width: 1366, height: 900 },
    { width: 390, height: 844 },
  ]) {
    const context = await browser.newContext({
      viewport,
      deviceScaleFactor: 1,
      acceptDownloads: true,
    });
    const page = await context.newPage();
    activePage = page;
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("http://127.0.0.1:8000/", { waitUntil: "networkidle" });
    assert.equal(await page.locator(".primary").isDisabled(), true);
    await page.screenshot({
      path: output + viewport.width + "-initial.png",
      fullPage: true,
    });
    await page
      .locator("input[type=file]")
      .setInputFiles(root + "data/fixtures/portrait.png");
    await page.waitForSelector(".source-preview");
    await page.getByRole("button", { name: "4×", exact: true }).click();
    assert.equal(await page.getByRole("button", { name: "4×", exact: true }).getAttribute("aria-pressed"), "true");
    assert((await page.locator(".estimate").innerText()).includes("640"));
    await page.getByRole("switch", { name: "人脸增强" }).check();
    assert.equal(await page.getByRole("switch", { name: "人脸增强" }).isChecked(), true);
    await page.getByRole("switch", { name: "人脸增强" }).uncheck();
    await page.getByLabel("输出格式", { exact: true }).selectOption("jpg");
    assert.equal(await page.getByLabel("质量", { exact: true }).isEnabled(), true);
    await page.getByLabel("输出格式", { exact: true }).selectOption("png");
    if (viewport.width > 500) await page.getByRole("button", { name: "2×", exact: true }).click();
    await page.locator(".primary").click();
    await page.waitForSelector(".result", { timeout: 180000 });
    await page.waitForFunction(() =>
      [...document.querySelectorAll(".compare-image")].every(
        (image) => image.complete && image.naturalWidth > 0,
      ),
    );
    await page
      .getByRole("button", { name: "100% 像素查看", exact: true })
      .click();
    const dimensions = await page
      .locator(".compare-image")
      .first()
      .evaluate((image) => ({
        natural: image.naturalWidth,
        display: image.getBoundingClientRect().width,
      }));
    assert.equal(dimensions.natural, dimensions.display);
    assert.equal(dimensions.natural, viewport.width > 500 ? 320 : 640);
    const surface = page.locator(".compare-viewport");
    await surface.scrollIntoViewIfNeeded();
    const box = await surface.boundingBox();
    await page.mouse.move(box.x + box.width * 0.55, box.y + box.height * 0.55);
    await page.mouse.down();
    await page.mouse.move(
      box.x + box.width * 0.55 + 32,
      box.y + box.height * 0.55 + 12,
    );
    await page.mouse.up();
    const transform = await page
      .locator(".compare-image")
      .first()
      .evaluate((image) => image.style.transform);
    assert(transform.includes("32px"));
    await page.locator(".split-range").focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator(".split-range").inputValue(), "51");
    const handle = await page.locator(".split-handle").boundingBox();
    await page.mouse.move(handle.x + 12, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.7, box.y + box.height / 2);
    await page.mouse.up();
    assert(Number(await page.locator(".split-range").inputValue()) > 65);
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await page.getByRole("button", { name: "适应窗口", exact: true }).click();
    await page.locator(".split-range").fill("50");
    await page.getByRole("heading", { name: "AI 图片超分", exact: true }).click();
    await page.screenshot({
      path: output + viewport.width + "-result.png",
      fullPage: true,
    });
    const downloadPromise = page.waitForEvent("download");
    await page.locator(".download").click();
    const download = await downloadPromise;
    await download.saveAs(output + viewport.width + "-download.png");
    assert.equal(await page.getByRole("button", { name: "重新处理", exact: true }).isEnabled(), true);
    await page.getByRole("button", { name: "更换图片", exact: true }).click();
    assert.equal(await page.locator(".result").count(), 0);
    assert.equal(await page.locator(".primary").isDisabled(), true);
    report.push({
      viewport,
      dimensions,
      download: download.suggestedFilename(),
      errors: [...errors],
    });
    await context.close();
  }
  const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
  const page = await context.newPage();
  activePage = page;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto("http://127.0.0.1:8000/", { waitUntil: "networkidle" });
  await page.locator("input[type=file]").setInputFiles(root + "data/fixtures/photo.jpg");
  await page.waitForSelector(".source-preview");
  const submitted = page.waitForResponse((response) => response.url().endsWith("/api/upscale") && response.request().method() === "POST");
  await page.locator(".primary").click();
  const created = await (await submitted).json();
  assert.equal(await page.getByRole("button", { name: "4×", exact: true }).isDisabled(), true);
  await page.getByRole("button", { name: "取消任务", exact: true }).click();
  await page.waitForFunction(async (id) => {
    const response = await fetch(`/api/upscale/${id}/status`);
    return (await response.json()).status === "cancelled";
  }, created.job_id, { timeout: 120000, polling: 1000 });
  await page.waitForFunction(() => document.querySelector(".primary")?.disabled === false);
  assert.equal(await page.locator(".download").count(), 0);
  report.push({ cancellation: "passed", job_id: created.job_id });
  await context.close();
  assert.deepEqual(errors, []);
  await writeFile(output + "report.json", JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  if (activePage && !activePage.isClosed()) {
    await activePage.screenshot({path:output + 'failure.png',fullPage:true});
    await writeFile(output + 'failure.json', JSON.stringify({error:String(error),errors,body:await activePage.locator('body').innerText()},null,2));
  }
  throw error;
} finally {
  await browser.close();
}
