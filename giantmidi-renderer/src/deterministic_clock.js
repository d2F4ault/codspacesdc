(() => {
  // Deterministic virtual clock + frame-stepped rAF.
  //
  // Verified against MIDIano source (js/player/Player.js, js/audio/AudioPlayer.js):
  //   AudioPlayer.getContextTime() = this.context.currentTime
  //   Player.playTick() delta = (currentTime - lastTime) * playbackSpeed
  //   skips the tick if delta < 0.0069 (stepping 1/60 = 0.01667 never trips)
  //   progress += min(0.1, delta) when not paused
  //   getTime() = progress + startDelay - scrollOffset
  //   startDelay = -2.5 // notes strike keys at getTime() == 0
  //   requestNextTick() = requestAnimationFrame(playTick)
  //   playTick() is already running from the Player constructor — Space flips paused=false via startPlay()/resume().
  //
  // Render.js is Canvas2D (cnv.getContext("2d")) driven off a separate rAF chain.
  // We own rAF so both chains advance together deterministically.
  //
  // Once __enterSteppedMode() is called, time only moves when Python calls
  // __advanceFrame(dt), by exactly one frame interval. 1x sync is exact by
  // construction: no setpts, no retiming, no global speed ratio.

  const NativeAC = window.AudioContext || window.webkitAudioContext;
  const nativeRAF = window.requestAnimationFrame.bind(window);
  const nativeCAF = (window.cancelAnimationFrame || function () {}).bind(window);
  const nativePerfNow = performance.now.bind(performance);
  const nativeDateNow = Date.now.bind(Date);

  window.__stepped = false;
  window.__rafQueue = [];
  window.__rafIdCounter = 1;
  window.__virtualAudioSeconds = 0;
  window.__acInstances = [];
  window.__perfFrozenAt = 0;
  window.__perfVirtual0 = 0;
  window.__dateFrozenAt = 0;
  window.__compositeCanvas = null;

  window.requestAnimationFrame = function (cb) {
    if (!window.__stepped) return nativeRAF(cb);
    const id = window.__rafIdCounter++;
    window.__rafQueue.push({ id: id, cb: cb });
    return id;
  };

  window.cancelAnimationFrame = function (id) {
    if (!window.__stepped) return nativeCAF(id);
    window.__rafQueue = window.__rafQueue.filter(function (e) { return e.id !== id; });
  };

  window.webkitRequestAnimationFrame = window.requestAnimationFrame;
  window.webkitCancelAnimationFrame = window.cancelAnimationFrame;

  performance.now = function () {
    if (!window.__stepped) return nativePerfNow();
    return window.__perfFrozenAt + (window.__virtualAudioSeconds * 1000 - window.__perfVirtual0);
  };

  Date.now = function () {
    if (!window.__stepped) return nativeDateNow();
    return window.__dateFrozenAt + (window.__virtualAudioSeconds * 1000 - window.__perfVirtual0);
  };

  function PatchedAC() {
    var args = Array.prototype.slice.call(arguments);
    var inst;
    if (typeof Reflect !== "undefined" && Reflect.construct) {
      inst = Reflect.construct(NativeAC, args);
    } else {
      inst = new NativeAC();
    }
    window.__acInstances.push(inst);
    var desc = Object.getOwnPropertyDescriptor(NativeAC.prototype, "currentTime");
    var nativeGetter = desc && desc.get;
    Object.defineProperty(inst, "currentTime", {
      get: function () {
        if (window.__stepped) return window.__virtualAudioSeconds;
        return nativeGetter ? nativeGetter.call(inst) : 0;
      },
      configurable: true,
    });
    var nativeResume = inst.resume.bind(inst);
    inst.resume = function () {
      if (window.__stepped) return Promise.resolve();
      return nativeResume();
    };
    return inst;
  }
  PatchedAC.prototype = NativeAC.prototype;
  try { Object.setPrototypeOf(PatchedAC, NativeAC); } catch (e) {}
  window.AudioContext = PatchedAC;
  window.webkitAudioContext = PatchedAC;

  window.__drainNativeRAF = function () {
    return new Promise(function (resolve) {
      nativeRAF(function () { nativeRAF(resolve); });
    });
  };

  window.__enterSteppedMode = function () {
    var seed = 0;
    if (window.__acInstances.length && NativeAC) {
      try {
        seed = Object.getOwnPropertyDescriptor(NativeAC.prototype, "currentTime").get.call(window.__acInstances[0]);
      } catch (e) { seed = 0; }
    }
    window.__virtualAudioSeconds = seed;
    window.__perfFrozenAt = nativePerfNow();
    window.__perfVirtual0 = seed * 1000;
    window.__dateFrozenAt = nativeDateNow();
    window.__rafQueue = [];
    window.__stepped = true;
    window.__acInstances.forEach(function (ctx) {
      try { ctx.suspend(); } catch (e) {}
    });
  };

  window.__advanceFrame = function (frameIntervalSeconds) {
    window.__virtualAudioSeconds += frameIntervalSeconds;
    var batch = window.__rafQueue;
    window.__rafQueue = [];
    var ts = window.__perfFrozenAt + (window.__virtualAudioSeconds * 1000 - window.__perfVirtual0);
    for (var i = 0; i < batch.length; i++) {
      try { batch[i].cb(ts); } catch (e) {}
    }
  };

  // canvas_composite: composite visible <canvas> nodes (PNG).
  // Midiano piano roll is Canvas2D.
  window.__captureFrame = function () {
    var canvases = Array.from(document.querySelectorAll("canvas")).filter(function (c) {
      return c.offsetParent !== null && c.style.display !== "none";
    });
    canvases.sort(function (a, b) {
      var za = parseInt(getComputedStyle(a).zIndex, 10) || 0;
      var zb = parseInt(getComputedStyle(b).zIndex, 10) || 0;
      return za - zb;
    });
    if (!window.__compositeCanvas) {
      window.__compositeCanvas = document.createElement("canvas");
    }
    var out = window.__compositeCanvas;
    if (out.width !== window.innerWidth || out.height !== window.innerHeight) {
      out.width = window.innerWidth;
      out.height = window.innerHeight;
    }
    var cctx = out.getContext("2d");
    cctx.fillStyle = "#000";
    cctx.fillRect(0, 0, out.width, out.height);
    for (var i = 0; i < canvases.length; i++) {
      try {
        var rect = canvases[i].getBoundingClientRect();
        cctx.drawImage(
          canvases[i],
          Math.round(rect.left),
          Math.round(rect.top),
          Math.round(rect.width),
          Math.round(rect.height)
        );
      } catch (e) {}
    }
    return out.toDataURL("image/png");
  };

  window.__captureThenStep = function (frameIntervalSeconds) {
    var data = window.__captureFrame();
    window.__advanceFrame(frameIntervalSeconds);
    return data;
  };
})();
