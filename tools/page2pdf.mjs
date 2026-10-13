/*
 * page2pdf.mjs -- archive a web page as a PDF with headless Chromium.
 *
 * Written for https://norand.io/mono/manual, whose downloadable PDF is no
 * longer available.  Drives Chromium over the DevTools protocol so the page
 * can be cleaned up before printing: the cookie banner, the fixed site nav
 * and the sticky table-of-contents sidebar all belong on a screen, not in a
 * printed document.
 *
 * Node 22+ only (global fetch and WebSocket, no dependencies).
 *
 *   node tools/page2pdf.mjs <url> <out.pdf> [--title "..."]
 */

import { spawn } from "node:child_process";
import { writeFileSync, existsSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME_CANDIDATES = [
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/opt/homebrew/bin/chromium",
  "chromium",
];

const PORT = 9333;

// Selectors removed before printing.  Anything fixed or sticky is furniture
// that would either float over the content or repeat on every page.
const REMOVE = [
  "#cookie-mask",          // the dimming overlay behind the cookie banner
  ".my-alert-chill",       // the cookie banner itself
  "nav.navbar",            // fixed site navigation
  ".manual-nav",           // sticky table of contents
  ".manual-rm-padding",    // the column that held it
  ".cookie-banner",
  "#onetrust-consent-sdk",
];

const PRINT_CSS = `
  body, .termsCondTop { padding-top: 0 !important; margin-top: 0 !important; }
  .row > div { flex: 0 0 100% !important; max-width: 100% !important; width: 100% !important; }
  img, svg { max-width: 100% !important; height: auto !important; }
  img, figure, table, pre, blockquote { break-inside: avoid; page-break-inside: avoid; }
  h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
  h1, h2 { break-before: auto; }
  a { text-decoration: none; }
`;

function pickChrome() {
  return CHROME_CANDIDATES.find((p) => {
    try {
      return existsSync(p);
    } catch {
      return false;
    }
  }) ?? "chromium";
}

class Cdp {
  #ws;
  #id = 0;
  #pending = new Map();
  #events = new Map();

  static async attach(wsUrl) {
    const cdp = new Cdp();
    cdp.#ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      cdp.#ws.onopen = res;
      cdp.#ws.onerror = rej;
    });
    cdp.#ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id !== undefined) {
        const p = cdp.#pending.get(msg.id);
        cdp.#pending.delete(msg.id);
        if (!p) return;
        msg.error ? p.rej(new Error(msg.error.message)) : p.res(msg.result);
      } else {
        (cdp.#events.get(msg.method) ?? []).forEach((fn) => fn(msg.params));
      }
    };
    return cdp;
  }

  send(method, params = {}) {
    const id = ++this.#id;
    return new Promise((res, rej) => {
      this.#pending.set(id, { res, rej });
      this.#ws.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, fn) {
    if (!this.#events.has(method)) this.#events.set(method, []);
    this.#events.get(method).push(fn);
  }

  close() {
    this.#ws.close();
  }
}

async function main() {
  const [url, out] = process.argv.slice(2);
  const titleArg = process.argv.includes("--title")
    ? process.argv[process.argv.indexOf("--title") + 1]
    : null;
  if (!url || !out) {
    console.error("usage: node tools/page2pdf.mjs <url> <out.pdf> [--title \"...\"]");
    process.exit(2);
  }

  const bin = pickChrome();
  console.log(`chromium      : ${bin}`);
  const chrome = spawn(bin, [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--user-data-dir=/tmp/page2pdf-profile",
    "--window-size=1200,1600",
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  chrome.stderr.on("data", () => {});

  // wait for the debugging endpoint
  let version = null;
  for (let i = 0; i < 60 && !version; i++) {
    try {
      version = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
    } catch {
      await sleep(250);
    }
  }
  if (!version) throw new Error("chromium did not come up");
  console.log(`browser       : ${version.Browser}`);

  const target = await (await fetch(
    `http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
  const cdp = await Cdp.attach(target.webSocketDebuggerUrl);

  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");

  let inflight = 0;
  cdp.on("Network.requestWillBeSent", () => inflight++);
  cdp.on("Network.loadingFinished", () => inflight--);
  cdp.on("Network.loadingFailed", () => inflight--);

  const loaded = new Promise((res) => cdp.on("Page.loadEventFired", res));
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 1200, height: 1600, deviceScaleFactor: 1, mobile: false,
  });
  console.log(`fetching      : ${url}`);
  await cdp.send("Page.navigate", { url });
  await loaded;

  // let late requests settle
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    if (inflight <= 0) break;
  }
  await sleep(1500);

  // scroll the whole document once, in case anything is revealed on scroll,
  // then go back to the top
  await cdp.send("Runtime.evaluate", {
    awaitPromise: true,
    expression: `(async () => {
      const step = window.innerHeight;
      for (let y = 0; y < document.body.scrollHeight; y += step) {
        window.scrollTo(0, y);
        await new Promise(r => setTimeout(r, 40));
      }
      window.scrollTo(0, 0);
    })()`,
  });
  await sleep(800);

  const clean = await cdp.send("Runtime.evaluate", {
    returnByValue: true,
    expression: `(() => {
      const removed = [];
      ${JSON.stringify(REMOVE)}.forEach(sel => {
        document.querySelectorAll(sel).forEach(el => { removed.push(sel); el.remove(); });
      });
      const st = document.createElement('style');
      st.textContent = ${JSON.stringify(PRINT_CSS)};
      document.head.appendChild(st);
      ${titleArg ? `document.title = ${JSON.stringify(titleArg)};` : ""}
      return {
        removed: [...new Set(removed)],
        height: document.body.scrollHeight,
        images: document.images.length,
        brokenImages: [...document.images].filter(i => !i.complete || i.naturalWidth === 0).length,
        title: document.title,
      };
    })()`,
  });
  const info = clean.result.value;
  console.log(`removed       : ${info.removed.join(", ") || "(nothing matched)"}`);
  console.log(`document      : ${info.height} px tall, ${info.images} images` +
              (info.brokenImages ? `, ${info.brokenImages} BROKEN` : ", all loaded"));

  // print with the screen stylesheet: the site has no print styles worth
  // honouring, and the screen layout is the one people know
  await cdp.send("Emulation.setEmulatedMedia", { media: "screen" });
  await sleep(300);

  const stamp = new Date().toISOString().slice(0, 10);
  const foot = `
    <div style="width:100%;font-size:8px;font-family:-apple-system,sans-serif;
                color:#888;padding:0 12mm;display:flex;justify-content:space-between;">
      <span>${url} &middot; archived ${stamp}</span>
      <span>page <span class="pageNumber"></span> / <span class="totalPages"></span></span>
    </div>`;

  // A3 portrait: (11.69 - 0.8) * 96 = 1045 css px of usable width, which
  // keeps the desktop layout above the 992 px breakpoint, so nothing has to
  // be scaled down and images print at their natural size
  const pdf = await cdp.send("Page.printToPDF", {
    printBackground: true,
    paperWidth: 11.69,
    paperHeight: 16.54,
    marginTop: 0.4,
    marginBottom: 0.55,
    marginLeft: 0.4,
    marginRight: 0.4,
    scale: 1,
    displayHeaderFooter: true,
    headerTemplate: "<div></div>",
    footerTemplate: foot,
    preferCSSPageSize: false,
  });

  const buf = Buffer.from(pdf.data, "base64");
  writeFileSync(out, buf);
  console.log(`wrote         : ${out} (${(buf.length / 1024 / 1024).toFixed(2)} MB)`);

  cdp.close();
  chrome.kill();
}

main().catch((e) => {
  console.error("failed:", e.message);
  process.exit(1);
});
