// The recording overlay for scripts/demo_video.py: a visible cursor, click rings, a spotlight and captions.
// Injected into the page only while recording; the website itself has none of this.
(() => {
  if (window.__demo) return;
  const css = `
    #demo-layer, #demo-layer * { box-sizing: border-box; pointer-events: none; }
    #demo-layer { position: fixed; inset: 0; z-index: 2147483647; font-family: "Plus Jakarta Sans", system-ui, sans-serif; }
    #demo-cursor { position: absolute; left: 0; top: 0; width: 26px; height: 26px; transform: translate(640px, 400px);
      transition: transform 650ms cubic-bezier(.45,.05,.25,1); filter: drop-shadow(0 2px 4px rgba(0,0,0,.6)); }
    .demo-ring { position: absolute; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 50%;
      border: 2px solid #00d68f; animation: demo-ring 700ms ease-out forwards; }
    @keyframes demo-ring { from { transform: scale(.4); opacity: 1 } to { transform: scale(3.2); opacity: 0 } }
    #demo-spot { position: absolute; border-radius: 14px; opacity: 0; transition: all 450ms ease;
      box-shadow: 0 0 0 9999px rgba(3,6,12,.62); outline: 2px solid #00d68f; outline-offset: 3px; }
    #demo-spot.on { opacity: 1; }
    #demo-spot.tone-block { outline-color: #ff4d6a; } #demo-spot.tone-hold { outline-color: #f5a623; }
    #demo-spot.tone-info { outline-color: #4ea8de; } #demo-spot.tone-admin { outline-color: #b197fc; }
    #demo-caption { position: absolute; left: 50%; bottom: 26px; max-width: min(980px, calc(100% - 48px));
      transform: translate(-50%, 12px); opacity: 0; transition: all 300ms ease; padding: 14px 22px 15px;
      background: rgba(12,21,36,.96); border: 1px solid #22344f; border-left: 3px solid #00d68f; border-radius: 14px;
      box-shadow: 0 12px 40px rgba(0,0,0,.5); }
    #demo-caption.on { opacity: 1; transform: translate(-50%, 0); }
    #demo-caption.top { bottom: auto; top: 72px; }
    #demo-caption .k { font: 600 11px/1 "IBM Plex Mono", monospace; letter-spacing: .12em; text-transform: uppercase;
      color: #00d68f; margin-bottom: 8px; }
    #demo-caption .t { font-size: 20px; line-height: 1.4; color: #e2eaf4; }
    #demo-fast { position: absolute; top: 62px; right: 16px; padding: 6px 10px; opacity: 0; transition: opacity 200ms;
      font: 600 11px/1 "IBM Plex Mono", monospace; letter-spacing: .12em; color: #060b14; background: #4ea8de;
      border-radius: 3px; }
    #demo-fast.on { opacity: .9; }
    #demo-title { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
      justify-content: center; text-align: center; background: #060b14; opacity: 0; transition: opacity 500ms ease;
      background-image: radial-gradient(50rem 32rem at 10% -10%, rgba(0,214,143,.14), transparent 60%),
        radial-gradient(44rem 30rem at 105% 110%, rgba(177,151,252,.12), transparent 60%); }
    #demo-title.on { opacity: 1; }
    #demo-title .k { font: 600 13px/1 "IBM Plex Mono", monospace; letter-spacing: .18em; text-transform: uppercase;
      color: #00d68f; }
    #demo-title .t { margin-top: 22px; font-size: 54px; font-weight: 700; letter-spacing: -.03em; color: #fff; }
    #demo-title .s { margin-top: 16px; max-width: 760px; font-size: 21px; line-height: 1.5; color: #8da3be; }
  `;
  const tones = { pass: "#00d68f", block: "#ff4d6a", hold: "#f5a623", info: "#4ea8de", admin: "#b197fc" };
  let layer, cursor, spot, caption, title, spotted = null;

  function mount() {
    if (layer || !document.body) return;
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);
    layer = document.createElement("div");
    layer.id = "demo-layer";
    layer.innerHTML = `
      <div id="demo-spot"></div>
      <svg id="demo-cursor" viewBox="0 0 24 24"><path d="M4 2l15 11-6.5 1.2L16 21l-3 1.4-3.4-6.9L4 20z"
        fill="#fff" stroke="#060b14" stroke-width="1.4" stroke-linejoin="round"/></svg>
      <div id="demo-fast">▸▸ SPED UP</div>
      <div id="demo-caption"><div class="k"></div><div class="t"></div></div>
      <div id="demo-title"><div class="k"></div><div class="t"></div><div class="s"></div></div>`;
    document.body.appendChild(layer);
    [cursor, spot, caption, title] = ["demo-cursor", "demo-spot", "demo-caption", "demo-title"].map((id) =>
      document.getElementById(id));
  }

  // Keep the spotlight on its element while the page scrolls or re-renders, and the caption out of its way
  function place() {
    let low = false;
    if (spotted && spotted.el.isConnected) {
      const r = spotted.el.getBoundingClientRect(), pad = spotted.pad;
      Object.assign(spot.style, { left: `${r.left - pad}px`, top: `${r.top - pad}px`,
        width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
      low = r.bottom > innerHeight - caption.offsetHeight - 50;
    }
    if (caption) caption.classList.toggle("top", low);
    requestAnimationFrame(place);
  }

  window.__demo = {
    move(x, y) { mount(); cursor.style.transform = `translate(${x - 4}px, ${y - 2}px)`; },
    ring(x, y, tone = "pass") {
      mount();
      const ring = document.createElement("div");
      ring.className = "demo-ring";
      Object.assign(ring.style, { left: `${x}px`, top: `${y}px`, borderColor: tones[tone] || tones.pass });
      layer.appendChild(ring);
      setTimeout(() => ring.remove(), 800);
    },
    spot(el, tone = "pass", pad = 6) {
      mount();
      spotted = { el, pad };
      spot.className = `on tone-${tone}`;
    },
    unspot() { mount(); spotted = null; spot.className = ""; },
    caption(kicker, text, tone = "pass") {
      mount();
      caption.querySelector(".k").textContent = kicker;
      caption.querySelector(".k").style.color = tones[tone];
      caption.style.borderLeftColor = tones[tone];
      caption.querySelector(".t").textContent = text;
      caption.className = "on";
    },
    uncaption() { mount(); caption.className = ""; },
    title(kicker, text, sub = "") {
      mount();
      title.querySelector(".k").textContent = kicker;
      title.querySelector(".t").textContent = text;
      title.querySelector(".s").textContent = sub;
      title.className = "on";
    },
    untitle() { mount(); title.className = ""; },
    // Shown only once a wait passes 2s, the point from which demo_video.py speeds it up
    fast(on) {
      mount();
      clearTimeout(this.fastTimer);
      const badge = document.getElementById("demo-fast");
      if (on) this.fastTimer = setTimeout(() => (badge.className = "on"), 2000);
      else badge.className = "";
    },
  };
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
  requestAnimationFrame(place);
})();
