// Hangout: the video-call screen shown once a voice call is answered.
// The ringing screen is still the old overlay (#voiceCallOverlay); after you
// pick up, this takes over the whole viewport: WhackGPT's tile, your tile,
// live captions, mute / hang up - and a shared screen. Anything visual the
// model makes during the call (draw_diagram, generate_image, edit_image,
// show_code) is "presented" on the stage while it keeps talking.
//
// script.js owns the call state (_call); this file only draws it.
//   Hangout.render(call)      - after every callSet()/callRender()
//   Hangout.presentSvg(svg)   - a drawing streaming in (partial markup)
//   Hangout.presentNode(el)   - a finished image / drawing element
//   Hangout.presentCode(lang, code)
//   Hangout.presentPending()  - "cooking" while an image is generated
//   Hangout.presentClear()    - an image failed - back to the faces
// Every present* call is a no-op unless the hangout is open.
var Hangout = (function () {
  let root = null, open = false, startedAt = 0, timer = null;

  // When Fernando starts screen sharing, some of the time the share opens on
  // the browser tab he forgot to close - Indeed, Kalshi, or worse:
  // robot porn (BoardHub, a Raspberry Pi as the "BWC-2005"; source in
  // fe/sites/src/) - before it cuts over to what he actually made. Real
  // sites refuse to be framed, so they are all screenshots (fe/sites/).
  const OOPS_ODDS = 0.8, OOPS_MS = 4000;
  const OOPS_SITES = [
    { title: "Now Hiring: 800 Ai Assistant Jobs | Indeed", url: "indeed.com/q-ai-assistant-jobs.html", img: "sites/indeed.jpg" },
    { title: "Kalshi - Prediction Market for Trading the Future", url: "kalshi.com", img: "sites/kalshi.jpg" },
  ];
  const XXX_SITES = [
    { title: "BWC-2005 — Fully Exposed, No Case, All 40 Pins | BoardHub", url: "boardhub.xxx/watch/bwc-2005", img: "sites/xxx-sbc.jpg" },
  ];
  // oopsUp: the tab is on screen. oopsTimer: its minimum time is still
  // running (anything arriving is held in oopsQueued). After that it stays
  // up until real content arrives - the "cooking" placeholder never replaces it.
  let oopsUp = false, oopsTimer = null, oopsQueued = null, oopsLast = null;

  // every site equally likely (never the same one twice in a row)
  function oopsSite() {
    const pool = OOPS_SITES.concat(XXX_SITES).filter((x) => x !== oopsLast);
    return (oopsLast = pool[Math.floor(Math.random() * pool.length)]);
  }

  // a browser window: a tab, an address bar, and the page (1280x800, scaled
  // to fit the presenting area)
  function showSite(b, site) {
    b.innerHTML =
      '<div class="ho-browser">' +
        '<div class="ho-br-tabs"><div class="ho-br-tab"><span></span><i class="fas fa-xmark"></i></div><i class="fas fa-plus"></i></div>' +
        '<div class="ho-br-bar"><i class="fas fa-arrow-left"></i><i class="fas fa-arrow-right"></i><i class="fas fa-rotate-right"></i>' +
          '<div class="ho-br-url"><i class="fas fa-lock"></i> <span></span></div></div>' +
        '<div class="ho-br-view"><div class="ho-br-page"></div></div>' +
      '</div>';
    b.querySelector(".ho-br-tab span").textContent = site.title;
    b.querySelector(".ho-br-url span").textContent = site.url;
    const pageEl = b.querySelector(".ho-br-page");
    const im = document.createElement("img");
    im.src = site.img;
    im.alt = "";
    pageEl.appendChild(im);
    const view = b.querySelector(".ho-br-view");
    // the whole page, scaled to fit - like a real shared screen
    const fit = () => {
      const w = view.clientWidth, h = view.clientHeight;
      const k = Math.min(1, w / 1280, h / 800);
      pageEl.style.transform = "translate(" + Math.max(0, (w - 1280 * k) / 2) + "px, 0) scale(" + k + ")";
      view.style.height = Math.ceil(800 * k) + "px";
    };
    fit();
    requestAnimationFrame(fit);
  }

  function oopsCancel() {
    clearTimeout(oopsTimer);
    oopsUp = false;
    oopsTimer = null;
    oopsQueued = null;
  }

  // Put something on the shared screen. A share that is just STARTING may
  // open on a forgotten tab first: it stays at least OOPS_MS, then until
  // something real (a drawing, an image, a document) arrives - the newest
  // of whatever came meanwhile. pending = the "cooking" placeholder.
  function show(fn, mayOops, pending, who) {
    if (!open) return;
    root.querySelector(".ho-presenter").textContent = who || "Fernando";
    if (oopsUp) {
      if (oopsTimer) { if (!pending) oopsQueued = fn; return; }
      if (pending) return; // the tab stands in for the placeholder
      oopsUp = false;
    }
    const starting = !root.classList.contains("presenting");
    setPresenting(true);
    const b = root.querySelector(".ho-present-body");
    if (starting && mayOops && Math.random() < OOPS_ODDS) {
      showSite(b, oopsSite());
      oopsUp = true;
      oopsQueued = pending ? null : fn;
      oopsTimer = setTimeout(() => {
        const q = oopsQueued;
        oopsTimer = null;
        oopsQueued = null;
        if (!open || !q) return; // nothing real yet - the tab stays up
        oopsUp = false;
        q(root.querySelector(".ho-present-body"));
      }, OOPS_MS);
      return;
    }
    fn(b);
  }

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function build() {
    root = el("div", "ho");
    root.id = "hangout";
    root.innerHTML =
      '<div class="ho-top">' +
        '<span class="ho-title">WhackGPT</span>' +
        '<span class="ho-dot">·</span><span class="ho-timer">00:00</span>' +
        '<span class="ho-rec"><i class="fas fa-circle"></i> REC</span>' +
      '</div>' +
      '<div class="ho-stage">' +
        '<div class="ho-present">' +
          '<div class="ho-present-label"><i class="fas fa-display"></i> <span class="ho-presenter">Fernando</span> is presenting</div>' +
          '<div class="ho-present-body"></div>' +
        '</div>' +
        '<div class="ho-tile ho-ai">' +
          '<img class="ho-face" src="whack-face.avif" alt="WhackGPT">' +
          '<div class="ho-name"><span class="ho-ai-name">Fernando</span> <i class="fas fa-microphone ho-mic"></i></div>' +
        '</div>' +
        '<div class="ho-tile ho-harriet">' +
          '<img class="ho-face" src="harriet.avif" alt="Carol Humbug">' +
          '<div class="ho-name">Carol · HR <i class="fas fa-microphone ho-mic"></i></div>' +
        '</div>' +
        '<div class="ho-tile ho-haruto">' +
          '<img class="ho-face" src="intern.avif" alt="Jacob">' +
          '<div class="ho-name">Jacob · intern <i class="fas fa-microphone-slash ho-mic"></i></div>' +
        '</div>' +
        '<div class="ho-tile ho-daddy">' +
          '<img class="ho-face" src="daddy.webp" alt="Daddy">' +
          '<div class="ho-name">Daddy <i class="fas fa-microphone ho-mic"></i></div>' +
        '</div>' +
        '<div class="ho-tile ho-fern">' +
          '<img class="ho-face" src="whack-face.avif" alt="Fernando">' +
          '<div class="ho-name">Fernando <i class="fas fa-microphone-slash ho-mic"></i></div>' +
        '</div>' +
        '<div class="ho-toast"></div>' +
        '<div class="ho-tile ho-me">' +
          '<div class="ho-initial">You</div>' +
          '<div class="ho-name">You <i class="fas fa-microphone ho-mic"></i></div>' +
        '</div>' +
      '</div>' +
      '<div class="ho-captions">' +
        '<div class="ho-cap ho-cap-me"></div>' +
        '<div class="ho-cap ho-cap-ai"></div>' +
      '</div>' +
      '<div class="ho-status"></div>' +
      '<div class="ho-controls">' +
        '<button class="ho-btn ho-mute" title="Mute"><i class="fas fa-microphone"></i></button>' +
        '<button class="ho-btn ho-hang" title="Leave call"><i class="fas fa-phone-slash"></i></button>' +
      '</div>';
    document.body.appendChild(root);
    root.querySelector(".ho-mute").addEventListener("click", () => callToggleMute());
    root.querySelector(".ho-hang").addEventListener("click", () => callEnd("you hung up"));
  }

  function tick() {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    root.querySelector(".ho-timer").textContent =
      String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  function setOpen(on) {
    if (on === open) return;
    open = on;
    if (on) {
      if (!root) build();
      startedAt = Date.now();
      tick();
      timer = setInterval(tick, 1000);
      setPresenting(false);
      root.classList.remove("harriet-here", "harriet-speaking", "haruto-quiet", "huddle", "daddy-here", "daddy-speaking");
      root.querySelector(".ho-cap-ai").textContent = "";
      root.querySelector(".ho-cap-me").textContent = "";
    } else {
      clearInterval(timer);
      timer = null;
      oopsCancel();
    }
    document.body.classList.toggle("in-hangout", on);
  }

  function setPresenting(on) {
    root.classList.toggle("presenting", on);
    if (!on) root.querySelector(".ho-present-body").innerHTML = "";
  }

  const STATUS = {
    listening: "speak now",
    muted: "you are muted",
    thinking: "Fernando is thinking…",
    tts: "Fernando is thinking…",
    speaking: "",
    harriet: "",
    haruto: "",
    daddy: "",
  };

  return {
    render(call) {
      const live = !!call && call.phase !== "ringing" && call.phase !== "ended";
      setOpen(live);
      if (!live) return;
      const p = call.phase;
      // the main tile is Jacob the intern until he hands over to Fernando
      const intern = !!call.intern;
      if (root.dataset.intern !== String(intern)) {
        root.dataset.intern = String(intern);
        root.querySelector(".ho-ai .ho-face").src = intern ? "intern.avif" : "whack-face.avif";
        root.querySelector(".ho-ai-name").textContent = intern ? "Jacob · intern" : "Fernando";
      }
      root.classList.toggle("ai-speaking", p === "speaking" || (p === "haruto" && intern));
      // summoned: the quiet intern unmutes for one line from his own tile
      root.classList.toggle("haruto-speaking", p === "haruto" && !intern);
      root.classList.toggle("harriet-here", !!call.harriet);
      root.classList.toggle("haruto-quiet", !!call.harutoQuiet);
      root.classList.toggle("huddle", !!call.huddle); // Fernando + Carol off in private
      root.classList.toggle("harriet-speaking", p === "harriet");
      root.classList.toggle("daddy-here", !!call.daddyHere); // pops in, then he's gone
      root.classList.toggle("daddy-speaking", p === "daddy");
      root.classList.toggle("me-speaking", p === "listening" && !!call.heard);
      root.classList.toggle("my-turn", p === "listening" && !call.heard);
      root.classList.toggle("me-muted", p === "muted");
      // newest words win - the caption area has a fixed height (see css)
      const heard = call.heard || "";
      root.querySelector(".ho-cap-me").textContent =
        heard ? "You: " + (heard.length > 110 ? "…" + heard.slice(-110) : heard) : "";
      // WhackGPT and Harriet are heard, not read - no captions for them
      root.querySelector(".ho-cap-ai").textContent = "";
      let status = STATUS[p] || "";
      if (p === "listening" && call.heard) status = ""; // they're talking
      if (p === "thinking" && call.harutoThinking) status = "Jacob is thinking…";
      else if (p === "thinking" && call.harrietThinking) status = "Carol is thinking…";
      else if (p === "thinking" && call.tokens) status += " " + call.tokens + " tokens";
      root.querySelector(".ho-status").textContent = status;
      const mute = root.querySelector(".ho-mute");
      mute.disabled = !(p === "listening" || p === "muted");
      mute.classList.toggle("muted", p === "muted");
      mute.innerHTML = p === "muted" ? '<i class="fas fa-microphone-slash"></i>' : '<i class="fas fa-microphone"></i>';
    },
    isOpen() { return open; },
    harrietJoined() { this._toast("Carol Humbug (HR, Mole Star) joined the call"); },
    harrietLeft() { this._toast("Carol Humbug left the call"); },
    fernandoJoined() { this._toast("Fernando joined the call"); },
    _toast(msg) {
      if (!open) return;
      const t = root.querySelector(".ho-toast");
      t.textContent = msg;
      t.classList.remove("show");
      void t.offsetWidth; // restart the animation
      t.classList.add("show");
    },
    presentSvg(svg) {
      const cut = (svg || "").lastIndexOf(">");
      if (cut < 0) return;
      const html = '<div class="ho-svg">' + _safeSvg(svg.slice(0, cut + 1)) + "</div>";
      show((b) => { b.innerHTML = html; }, true);
    },
    presentNode(node, noOops, who) {
      if (!node) return;
      show((b) => { b.innerHTML = ""; b.appendChild(node); }, !noOops, false, who);
    },
    presentCode(lang, code) {
      show((b) => {
        const pre = el("pre", "ho-code");
        pre.textContent = code || "";
        b.innerHTML = "";
        if (lang) b.appendChild(el("div", "ho-code-lang", null)).textContent = lang;
        b.appendChild(pre);
      }, false);
    },
    presentPending() {
      show((b) => { b.innerHTML = '<img class="ho-pending" src="norms.avif" alt="cooking…">'; }, true, true);
    },
    presentClear() {
      if (!open) return;
      oopsCancel(); // an image failed - the tab goes too
      setPresenting(false);
    },
  };
})();
