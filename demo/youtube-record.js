const puppeteer = require("puppeteer");
const path = require("path");
const { execSync } = require("child_process");

const TOTAL_SLIDES = 12;
const SECONDS_PER_SLIDE = 15;
const FPS = 30;
const FRAMES_PER_SLIDE = SECONDS_PER_SLIDE * FPS;
const TOTAL_FRAMES = TOTAL_SLIDES * FRAMES_PER_SLIDE;
const OUTPUT_DIR = path.join(__dirname, "yt-frames");
const OUTPUT_VIDEO = path.resolve(__dirname, "kodiak-youtube.mp4");

async function main() {
  console.log("Recording Kodiak YouTube demo video...\n");
  console.log(`${TOTAL_SLIDES} slides x ${SECONDS_PER_SLIDE}s = ${TOTAL_SLIDES * SECONDS_PER_SLIDE}s (${(TOTAL_SLIDES * SECONDS_PER_SLIDE / 60).toFixed(1)} min)`);

  execSync(`rm -rf "${OUTPUT_DIR}" && mkdir -p "${OUTPUT_DIR}"`);

  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--window-size=1920,1080"],
    defaultViewport: { width: 1920, height: 1080 },
  });

  const page = await browser.newPage();
  await page.goto(`file://${path.join(__dirname, "youtube-presentation.html")}`, {
    waitUntil: "domcontentloaded",
  });

  // Stop the auto-advance timer
  await page.evaluate(() => {
    const highestId = setTimeout(() => {}, 0);
    for (let i = 0; i < highestId; i++) clearTimeout(i);
    for (let i = 0; i < highestId; i++) clearInterval(i);
  });

  let frameIndex = 0;

  for (let slide = 0; slide < TOTAL_SLIDES; slide++) {
    // Show this slide
    await page.evaluate((n) => {
      document.querySelectorAll('.slide').forEach(s => s.classList.remove('active'));
      const sl = document.getElementById(`slide-${n + 1}`);
      if (sl) {
        sl.querySelectorAll('.fade-item').forEach(e => e.classList.remove('visible'));
        sl.classList.add('active');
      }
      const total = document.querySelectorAll('.slide').length;
      document.getElementById('progress').style.width = `${((n + 1) / total) * 100}%`;
      document.getElementById('slide-number').textContent = `${n + 1} / ${total}`;
    }, slide);

    await new Promise(r => setTimeout(r, 200));

    // Trigger fade-in items
    const fadeItemCount = await page.evaluate((n) => {
      const sl = document.getElementById(`slide-${n + 1}`);
      return sl ? sl.querySelectorAll('.fade-item').length : 0;
    }, slide);

    const fadeInterval = fadeItemCount > 0 ? Math.min(600, 3000 / fadeItemCount) : 0;
    let fadeTriggered = 0;

    for (let f = 0; f < FRAMES_PER_SLIDE; f++) {
      const elapsedMs = (f / FPS) * 1000;
      while (fadeTriggered < fadeItemCount && elapsedMs > (fadeTriggered + 1) * fadeInterval) {
        await page.evaluate((slideIdx, itemIdx) => {
          const sl = document.getElementById(`slide-${slideIdx + 1}`);
          if (sl) {
            const items = sl.querySelectorAll('.fade-item');
            if (items[itemIdx]) items[itemIdx].classList.add('visible');
          }
        }, slide, fadeTriggered);
        fadeTriggered++;
      }

      const frameNum = String(frameIndex).padStart(6, "0");
      await page.screenshot({
        path: path.join(OUTPUT_DIR, `frame_${frameNum}.png`),
        type: "png",
      });
      frameIndex++;
    }

    console.log(`  Slide ${slide + 1}/${TOTAL_SLIDES} captured (${FRAMES_PER_SLIDE} frames)`);
  }

  await browser.close();
  console.log(`\n${frameIndex} frames captured. Encoding video...`);

  execSync(
    `ffmpeg -y -framerate ${FPS} -i "${OUTPUT_DIR}/frame_%06d.png" ` +
      `-c:v libx264 -pix_fmt yuv420p -crf 18 -preset medium ` +
      `"${OUTPUT_VIDEO}"`,
    { stdio: "inherit" }
  );

  execSync(`rm -rf "${OUTPUT_DIR}"`);

  const duration = TOTAL_SLIDES * SECONDS_PER_SLIDE;
  console.log(`\nDone! Video saved to: ${OUTPUT_VIDEO}`);
  console.log(`Duration: ${duration}s (${(duration / 60).toFixed(1)} min)`);
  console.log(`Slides: ${TOTAL_SLIDES} x ${SECONDS_PER_SLIDE}s each`);
}

main().catch(console.error);
