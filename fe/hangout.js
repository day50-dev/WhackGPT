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
          '<div class="ho-present-label"><i class="fas fa-display"></i> Fernando is presenting</div>' +
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
          '<img class="ho-face" src="intern.avif" alt="Haruto">' +
          '<div class="ho-name">Haruto · intern <i class="fas fa-microphone-slash ho-mic"></i></div>' +
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
      root.classList.remove("harriet-here", "harriet-speaking", "haruto-quiet");
      root.querySelector(".ho-cap-ai").textContent = "";
      root.querySelector(".ho-cap-me").textContent = "";
    } else {
      clearInterval(timer);
      timer = null;
    }
    document.body.classList.toggle("in-hangout", on);
  }

  function setPresenting(on) {
    root.classList.toggle("presenting", on);
    if (!on) root.querySelector(".ho-present-body").innerHTML = "";
  }

  function body() {
    if (!open) return null;
    setPresenting(true);
    return root.querySelector(".ho-present-body");
  }

  const STATUS = {
    listening: "speak now",
    muted: "you are muted",
    thinking: "Fernando is thinking…",
    tts: "Fernando is thinking…",
    speaking: "",
    harriet: "",
    haruto: "",
  };

  return {
    render(call) {
      const live = !!call && call.phase !== "ringing" && call.phase !== "ended";
      setOpen(live);
      if (!live) return;
      const p = call.phase;
      // the main tile is Haruto the intern until he hands over to Fernando
      const intern = !!call.intern;
      if (root.dataset.intern !== String(intern)) {
        root.dataset.intern = String(intern);
        root.querySelector(".ho-ai .ho-face").src = intern ? "intern.avif" : "whack-face.avif";
        root.querySelector(".ho-ai-name").textContent = intern ? "Haruto · intern" : "Fernando";
      }
      root.classList.toggle("ai-speaking", p === "speaking" || p === "haruto");
      root.classList.toggle("harriet-here", !!call.harriet);
      root.classList.toggle("haruto-quiet", !!call.harutoQuiet);
      root.classList.toggle("harriet-speaking", p === "harriet");
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
      if (p === "thinking" && call.harutoThinking) status = "Haruto is thinking…";
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
      const b = body();
      if (!b) return;
      const cut = (svg || "").lastIndexOf(">");
      if (cut < 0) return;
      b.innerHTML = '<div class="ho-svg">' + _safeSvg(svg.slice(0, cut + 1)) + "</div>";
    },
    presentNode(node) {
      const b = body();
      if (!b || !node) return;
      b.innerHTML = "";
      b.appendChild(node);
    },
    presentCode(lang, code) {
      const b = body();
      if (!b) return;
      const pre = el("pre", "ho-code");
      pre.textContent = code || "";
      b.innerHTML = "";
      if (lang) b.appendChild(el("div", "ho-code-lang", null)).textContent = lang;
      b.appendChild(pre);
    },
    presentPending() {
      const b = body();
      if (!b) return;
      b.innerHTML = '<img class="ho-pending" src="norms.avif" alt="cooking…">';
    },
    presentClear() {
      if (open) setPresenting(false);
    },
  };
})();
