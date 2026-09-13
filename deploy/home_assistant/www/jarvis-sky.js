/*
 * jarvis-sky — paints the real sky behind the Home Assistant dashboard.
 *
 * The themes give the palette; this gives it a sun in the right place, a moon
 * in its actual phase, stars that fade as cloud rolls in, and rain when it is
 * raining. It repaints every few minutes, which is as "live" as a sky needs to
 * be, and never on a timer faster than the sky itself changes.
 *
 * Two decisions worth knowing about:
 *
 * The sun's position is taken from Home Assistant (sun.sun carries an exact
 * elevation and azimuth), while the moon's is computed here. Home Assistant
 * has no moon position, and the standard low-precision formulae are accurate
 * to a fraction of a degree -- far better than anything visible on a 7" panel.
 *
 * The background is applied to the element that actually paints it, found by
 * walking the shadow DOM at runtime rather than assuming a CSS variable. The
 * variable this would otherwise use, --lovelace-background, could not be found
 * in the frontend this instance serves, and a theme variable that no version
 * consumes fails silently. If the walk finds nothing, the module does nothing
 * and the themes underneath still stand.
 */

(() => {
  "use strict";

  const VERSION = "jarvis-sky v3";
  const TICK_MS = 5 * 60 * 1000;   // the sky does not move faster than this
  const WATCH_MS = 30 * 1000;      // but weather can change between ticks
  const WEATHER = "weather.forecast_dom";

  const RAIN = ["rainy", "pouring", "lightning", "lightning-rainy", "hail"];
  const SNOW = ["snowy", "snowy-rainy"];
  const FOG = ["fog"];

  // ---------------------------------------------------------------- astronomy

  const rad = Math.PI / 180;
  const dayMs = 86400000;
  const J1970 = 2440588;
  const J2000 = 2451545;
  const obliquity = rad * 23.4397;

  const toDays = (date) => date.valueOf() / dayMs - 0.5 + J1970 - J2000;

  const rightAscension = (l, b) =>
    Math.atan2(Math.sin(l) * Math.cos(obliquity) - Math.tan(b) * Math.sin(obliquity), Math.cos(l));
  const declination = (l, b) =>
    Math.asin(Math.sin(b) * Math.cos(obliquity) + Math.cos(b) * Math.sin(obliquity) * Math.sin(l));
  const siderealTime = (d, lw) => rad * (280.16 + 360.9856235 * d) - lw;

  function sunCoords(d) {
    const M = rad * (357.5291 + 0.98560028 * d);
    const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
    const L = M + C + rad * 102.9372 + Math.PI;
    return { dec: declination(L, 0), ra: rightAscension(L, 0) };
  }

  function moonCoords(d) {
    const L = rad * (218.316 + 13.176396 * d);   // ecliptic longitude
    const M = rad * (134.963 + 13.064993 * d);   // mean anomaly
    const F = rad * (93.272 + 13.229350 * d);    // mean distance from ascending node
    const l = L + rad * 6.289 * Math.sin(M);
    const b = rad * 5.128 * Math.sin(F);
    const dist = 385001 - 20905 * Math.cos(M);
    return { ra: rightAscension(l, b), dec: declination(l, b), dist };
  }

  /** Illuminated fraction, and whether the moon is waxing. */
  function moonIllumination(date) {
    const d = toDays(date);
    const s = sunCoords(d);
    const m = moonCoords(d);
    const sunDist = 149598000;
    const phi = Math.acos(
      Math.sin(s.dec) * Math.sin(m.dec) +
      Math.cos(s.dec) * Math.cos(m.dec) * Math.cos(s.ra - m.ra)
    );
    const inc = Math.atan2(sunDist * Math.sin(phi), m.dist - sunDist * Math.cos(phi));
    const angle = Math.atan2(
      Math.cos(s.dec) * Math.sin(s.ra - m.ra),
      Math.sin(s.dec) * Math.cos(m.dec) - Math.cos(s.dec) * Math.sin(m.dec) * Math.cos(s.ra - m.ra)
    );
    return { fraction: (1 + Math.cos(inc)) / 2, waxing: angle < 0 };
  }

  /** Where the moon is in the sky, in degrees. */
  function moonPosition(date, lat, lon) {
    const lw = rad * -lon;
    const phi = rad * lat;
    const d = toDays(date);
    const c = moonCoords(d);
    const H = siderealTime(d, lw) - c.ra;
    let h = Math.asin(
      Math.sin(phi) * Math.sin(c.dec) + Math.cos(phi) * Math.cos(c.dec) * Math.cos(H)
    );
    // Atmospheric refraction lifts a low moon by about half a degree.
    h += (rad * 0.017) / Math.tan(h + (rad * 10.26) / (h + rad * 5.1));
    const az = Math.atan2(
      Math.sin(H),
      Math.cos(H) * Math.sin(phi) - Math.tan(c.dec) * Math.cos(phi)
    );
    return { altitude: h / rad, azimuth: ((az / rad) + 180) % 360 };
  }

  // ------------------------------------------------------------------ drawing

  /** Sky as seen facing south: east on the left, west on the right. */
  function skyPosition(azimuthDeg, altitudeDeg) {
    let x = ((azimuthDeg - 90) / 180) * 100;
    x = Math.max(-12, Math.min(112, x));
    const y = 92 - Math.max(0, Math.min(75, altitudeDeg)) * 1.15;
    return { x, y };
  }

  const lerp = (a, b, t) => a + (b - a) * t;

  function mixHex(from, to, t) {
    const parse = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    const [r1, g1, b1] = parse(from);
    const [r2, g2, b2] = parse(to);
    const to2 = (v) => Math.round(v).toString(16).padStart(2, "0");
    return `#${to2(lerp(r1, r2, t))}${to2(lerp(g1, g2, t))}${to2(lerp(b1, b2, t))}`;
  }

  // Sky colours keyed to the sun's elevation: top of the dome, mid, horizon.
  const PALETTE = [
    { at: -18, top: "#04050a", mid: "#05070c", low: "#080b14" },
    { at: -10, top: "#06080f", mid: "#0a0f1c", low: "#131b30" },
    { at: -6, top: "#0a0f1c", mid: "#16203a", low: "#2a3350" },
    { at: -2, top: "#152142", mid: "#3c3a5e", low: "#7d5560" },
    { at: 1, top: "#28406d", mid: "#6a5a7a", low: "#d1855a" },
    { at: 5, top: "#3f6a9e", mid: "#8f9aae", low: "#e6b483" },
    { at: 12, top: "#5a90c0", mid: "#a3c2dd", low: "#dfe6ea" },
    { at: 35, top: "#4a86c4", mid: "#9cc3e2", low: "#dce8ee" },
    { at: 70, top: "#3f7cc0", mid: "#93bede", low: "#d8e6ef" },
  ];

  function skyColours(elevation) {
    if (elevation <= PALETTE[0].at) return PALETTE[0];
    const last = PALETTE[PALETTE.length - 1];
    if (elevation >= last.at) return last;
    for (let i = 0; i < PALETTE.length - 1; i++) {
      const a = PALETTE[i];
      const b = PALETTE[i + 1];
      if (elevation >= a.at && elevation <= b.at) {
        const t = (elevation - a.at) / (b.at - a.at);
        return {
          top: mixHex(a.top, b.top, t),
          mid: mixHex(a.mid, b.mid, t),
          low: mixHex(a.low, b.low, t),
        };
      }
    }
    return last;
  }

  /** A fixed star field. Seeded, so stars never jump between repaints. */
  const STARS = (() => {
    let seed = 20260823;
    const random = () => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };
    return Array.from({ length: 90 }, () => {
      const y = Math.pow(random(), 1.5) * 78;      // denser high up
      return {
        x: random() * 100,
        y,
        size: 0.7 + random() * 1.0,
        brightness: 0.25 + random() * 0.6,
      };
    });
  })();

  function starLayers(strength) {
    if (strength <= 0.02) return [];
    return STARS.map((s) => {
      const a = (s.brightness * strength).toFixed(2);
      return `radial-gradient(${s.size}px ${s.size}px at ${s.x.toFixed(1)}% ${s.y.toFixed(1)}%, rgba(255,255,255,${a}), rgba(255,255,255,0) 100%)`;
    });
  }

  /** The moon, in tonight's real phase, as an inline SVG layer.
   *
   * `alpha` fades the whole thing: a daytime moon is genuinely there, just
   * pale, so it is drawn faintly rather than hidden. The glow scales with how
   * much of the disc is lit -- a thin crescent barely lights anything, and a
   * full moon washes out the stars around it.
   */
  function moonLayer(date, lat, lon, alpha) {
    const pos = moonPosition(date, lat, lon);
    if (pos.altitude < -1 || alpha < 0.04) return null;

    const { fraction, waxing } = moonIllumination(date);
    const r = 30;
    const box = 84;
    const c = box / 2;
    // The terminator is an ellipse; its x-radius is signed, so the same path
    // draws a crescent and a gibbous depending on how much is lit. The sweep
    // flag decides which way that arc bulges, and getting it backwards drew a
    // 17% crescent where 83% was wanted -- measured, not guessed: filling the
    // path and counting pixels gives 0.832 for sweep=1 and 0.172 for sweep=0.
    const rx = Math.abs(r * (1 - 2 * fraction)).toFixed(2);
    const sweep = fraction < 0.5 ? 0 : 1;
    const lit =
      `M ${c},${c - r} A ${r},${r} 0 0,1 ${c},${c + r} ` +
      `A ${rx},${r} 0 0,${sweep} ${c},${c - r} Z`;
    const flip = waxing ? "" : ` transform="translate(${box},0) scale(-1,1)"`;
    const a = alpha.toFixed(2);

    // Limb darkening plus two soft grey patches: enough that it reads as the
    // moon rather than a white circle, without pretending to be a photograph.
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="${box}" height="${box}" viewBox="0 0 ${box} ${box}">` +
      `<defs>` +
      `<radialGradient id="d" cx="42%" cy="38%" r="62%">` +
      `<stop offset="0%" stop-color="#fbf8ef" stop-opacity="${a}"/>` +
      `<stop offset="72%" stop-color="#e8e3d3" stop-opacity="${a}"/>` +
      `<stop offset="100%" stop-color="#c9c3b1" stop-opacity="${a}"/>` +
      `</radialGradient>` +
      `<clipPath id="c"><path d="${lit}"/></clipPath>` +
      `</defs>` +
      `<g${flip}>` +
      // Earthshine: on a slim crescent the unlit disc really is faintly
      // visible, lit by sunlight bouncing off the earth. It fades out as the
      // moon fills, which is also what happens outside.
      (fraction < 0.45
        ? `<circle cx="${c}" cy="${c}" r="${r}" fill="#8e9bb4" opacity="${(alpha * 0.16 * (1 - fraction / 0.45)).toFixed(3)}"/>`
        : "") +
      `<path d="${lit}" fill="url(#d)"/>` +
      `<g clip-path="url(#c)" opacity="${(alpha * 0.5).toFixed(2)}">` +
      `<ellipse cx="${c - 6}" cy="${c - 7}" rx="9" ry="7" fill="#b9b3a2"/>` +
      `<ellipse cx="${c + 7}" cy="${c + 6}" rx="7" ry="9" fill="#bdb7a6"/>` +
      `<ellipse cx="${c - 2}" cy="${c + 11}" rx="5" ry="4" fill="#c2bcab"/>` +
      `</g></g></svg>`;

    const uri = `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
    const { x, y } = skyPosition(pos.azimuth, pos.altitude);
    const size = 48;
    const glowAlpha = (0.20 * fraction * alpha).toFixed(3);
    return {
      layer: `${uri} ${x.toFixed(1)}% ${y.toFixed(1)}% / ${size}px ${size}px no-repeat`,
      glow: `radial-gradient(circle ${Math.round(90 + 90 * fraction)}px at ${x.toFixed(1)}% ${y.toFixed(1)}%, rgba(198,212,240,${glowAlpha}), rgba(198,212,240,0) 72%)`,
      fraction,
      waxing,
      altitude: pos.altitude,
      azimuth: pos.azimuth,
    };
  }

  function sunGlow(elevation, azimuth) {
    if (elevation < -8) return null;
    const { x, y } = skyPosition(azimuth, elevation);
    // Low sun burns orange and spreads; high sun is small, pale and bright.
    const warmth = Math.max(0, Math.min(1, (8 - elevation) / 16));
    const colour = elevation < 4 ? "255, 176, 108" : "255, 238, 196";
    const alpha = (0.55 - warmth * 0.1).toFixed(2);
    const w = Math.round(lerp(420, 700, warmth));
    const h = Math.round(lerp(300, 420, warmth));
    return `radial-gradient(${w}px ${h}px at ${x.toFixed(1)}% ${y.toFixed(1)}%, rgba(${colour},${alpha}), rgba(${colour},0) 70%)`;
  }

  function cloudLayers(cover, elevation) {
    if (!cover || cover < 12) return [];
    const t = Math.min(1, cover / 100);
    const light = elevation > 0;
    const tint = light ? "236, 240, 244" : "128, 140, 158";
    const a = (t * (light ? 0.5 : 0.22)).toFixed(2);
    return [
      `radial-gradient(760px 240px at 22% ${light ? 26 : 30}%, rgba(${tint},${a}), rgba(${tint},0) 72%)`,
      `radial-gradient(620px 200px at 74% 16%, rgba(${tint},${(t * (light ? 0.42 : 0.18)).toFixed(2)}), rgba(${tint},0) 72%)`,
      `radial-gradient(900px 260px at 50% 62%, rgba(${tint},${(t * (light ? 0.34 : 0.14)).toFixed(2)}), rgba(${tint},0) 74%)`,
    ];
  }

  // ------------------------------------------------------------ precipitation

  const OVERLAY_ID = "jarvis-sky-overlay";
  const STYLE_ID = "jarvis-sky-style";

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      #${OVERLAY_ID} {
        position: fixed; inset: 0; pointer-events: none; z-index: 3;
        overflow: hidden; opacity: 0; transition: opacity 2s ease;
      }
      #${OVERLAY_ID} .drop {
        position: absolute; top: -14vh; width: 1px; border-radius: 1px;
        background: linear-gradient(to bottom, rgba(198,220,236,0), rgba(198,220,236,0.55));
        animation: jarvis-fall linear infinite;
      }
      #${OVERLAY_ID} .flake {
        position: absolute; top: -6vh; border-radius: 50%;
        background: rgba(255,255,255,0.8);
        animation: jarvis-drift linear infinite;
      }
      @keyframes jarvis-fall {
        from { transform: translate3d(0, -14vh, 0); }
        to   { transform: translate3d(-4vw, 114vh, 0); }
      }
      @keyframes jarvis-drift {
        from { transform: translate3d(0, -6vh, 0); }
        to   { transform: translate3d(6vw, 106vh, 0); }
      }
      @media (prefers-reduced-motion: reduce) {
        #${OVERLAY_ID} .drop, #${OVERLAY_ID} .flake { animation: none; display: none; }
      }
    `;
    document.head.appendChild(style);
  }

  function ensureOverlay() {
    let el = document.getElementById(OVERLAY_ID);
    if (!el) {
      el = document.createElement("div");
      el.id = OVERLAY_ID;
      document.body.appendChild(el);
    }
    return el;
  }

  let currentPrecipitation = null;

  function setPrecipitation(kind) {
    if (kind === currentPrecipitation) return;
    currentPrecipitation = kind;
    ensureStyle();
    const el = ensureOverlay();
    el.innerHTML = "";
    if (!kind) {
      el.style.opacity = "0";
      return;
    }
    const count = kind === "snow" ? 40 : 60;
    for (let i = 0; i < count; i++) {
      const bit = document.createElement("div");
      const left = Math.random() * 104 - 2;
      if (kind === "snow") {
        const size = 2 + Math.random() * 3;
        bit.className = "flake";
        bit.style.cssText =
          `left:${left}%;width:${size}px;height:${size}px;` +
          `opacity:${(0.35 + Math.random() * 0.45).toFixed(2)};` +
          `animation-duration:${(7 + Math.random() * 7).toFixed(1)}s;` +
          `animation-delay:-${(Math.random() * 12).toFixed(1)}s;`;
      } else {
        bit.className = "drop";
        bit.style.cssText =
          `left:${left}%;height:${(40 + Math.random() * 70).toFixed(0)}px;` +
          `opacity:${(0.2 + Math.random() * 0.4).toFixed(2)};` +
          `animation-duration:${(0.6 + Math.random() * 0.55).toFixed(2)}s;` +
          `animation-delay:-${(Math.random() * 2).toFixed(2)}s;`;
      }
      el.appendChild(bit);
    }
    el.style.opacity = kind === "snow" ? "0.85" : "0.7";
  }

  // ------------------------------------------------------------ wiring to HA

  /** Depth-first walk through shadow roots for the element painting the view. */
  function findViewElement(root = document.body, depth = 0) {
    if (!root || depth > 12) return null;
    const direct = root.querySelector?.("#view, hui-view, .view");
    if (direct) return direct;
    const kids = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const kid of kids) {
      if (kid.shadowRoot) {
        const found = findViewElement(kid.shadowRoot, depth + 1);
        if (found) return found;
      }
    }
    return null;
  }

  const getHass = () => document.querySelector("home-assistant")?.hass ?? null;

  const state = { last: null, target: null, painted: 0, error: null };

  function compose(hass) {
    const sun = hass.states["sun.sun"];
    const weather = hass.states[WEATHER];
    const elevation = Number(sun?.attributes?.elevation ?? -30);
    const azimuth = Number(sun?.attributes?.azimuth ?? 180);
    const condition = weather?.state ?? "unknown";
    const cover = Number(weather?.attributes?.cloud_coverage ?? 0);
    const lat = Number(hass.config?.latitude ?? 45.74);
    const lon = Number(hass.config?.longitude ?? 15.89);
    const now = new Date();

    const colours = skyColours(elevation);
    const layers = [];

    // Stars fade out as the sun comes up and as cloud thickens.
    const darkness = Math.max(0, Math.min(1, (-2 - elevation) / 10));
    const clearness = 1 - Math.min(1, cover / 100) * 0.92;

    // A daytime moon is real, just pale, and cloud hides it like anything else.
    const daylight = Math.max(0, Math.min(1, (elevation + 4) / 14));
    const moonAlpha = (1 - daylight * 0.78) * (1 - Math.min(1, cover / 100) * 0.85);
    const moon = moonLayer(now, lat, lon, moonAlpha);
    if (moon) layers.push(moon.layer, moon.glow);

    // A bright moon washes out the faint stars near it, as it does outdoors.
    const moonWash = moon && moon.altitude > 0 ? 1 - moon.fraction * 0.45 : 1;
    layers.push(...starLayers(darkness * clearness * moonWash));

    const glow = sunGlow(elevation, azimuth);
    if (glow) layers.push(glow);
    layers.push(...cloudLayers(cover, elevation));
    if (FOG.includes(condition)) {
      layers.push("linear-gradient(180deg, rgba(190,196,202,0.30), rgba(190,196,202,0.55))");
    }
    layers.push(
      `linear-gradient(180deg, ${colours.top} 0%, ${colours.mid} 55%, ${colours.low} 100%)`
    );

    const precipitation = RAIN.includes(condition)
      ? "rain"
      : SNOW.includes(condition)
        ? "snow"
        : null;

    return {
      css: layers.join(", "),
      precipitation,
      debug: {
        elevation, azimuth, condition, cover,
        moon: moon
          ? {
              altitude: Number(moon.altitude.toFixed(1)),
              azimuth: Number(moon.azimuth.toFixed(1)),
              lit: Math.round(moon.fraction * 100) + "%",
              phase: moon.waxing ? "raste" : "opada",
            }
          : "ispod horizonta",
        layers: layers.length,
      },
    };
  }

  function paint() {
    const hass = getHass();
    if (!hass) return;
    let composed;
    try {
      composed = compose(hass);
    } catch (err) {
      state.error = String(err);
      return;
    }

    const view = findViewElement();
    state.target = view ? (view.id || view.localName) : null;
    if (view) {
      view.style.setProperty("background", composed.css, "important");
      view.style.setProperty("background-attachment", "fixed", "important");
    }
    // Belt and braces: whichever variable this frontend honours, it gets the
    // same sky, and a future version that starts using it needs no change here.
    document.documentElement.style.setProperty("--lovelace-background", composed.css);

    setPrecipitation(composed.precipitation);
    state.last = composed.debug;
    state.painted = Date.now();
  }

  function watch() {
    const hass = getHass();
    if (!hass) return;
    const weather = hass.states[WEATHER];
    const signature = `${weather?.state}|${weather?.attributes?.cloud_coverage}`;
    if (signature !== watch.signature) {
      watch.signature = signature;
      paint();
    }
  }

  /* Say hello in Home Assistant's own log, once per page load.
   *
   * There is no other way to find out whether this ran on someone else's
   * screen. A phone reported "it does nothing" and the possibilities -- the
   * file never fetched, the shadow-DOM walk finding nothing, the maths
   * throwing -- are indistinguishable from the outside. One line in the log
   * separates them, and the absence of a line is itself the answer.
   */
  function report() {
    const hass = getHass();
    if (!hass?.callService) return;
    const ua = (navigator.userAgent || "").slice(0, 90);
    const found = state.target || "NIJE NAĐEN";
    const layers = state.last ? state.last.layers : "-";
    hass.callService("system_log", "write", {
      level: "warning",
      logger: "jarvis_sky",
      message: `${VERSION} | element=${found} | slojeva=${layers} | ${ua}`,
    }).catch(() => {});
  }

  function start() {
    if (!getHass()) {
      setTimeout(start, 1000);
      return;
    }
    paint();
    setTimeout(report, 1500);
    setInterval(paint, TICK_MS);
    setInterval(watch, WATCH_MS);
    // The view element is rebuilt when the dashboard is re-rendered, which
    // drops the inline background; repainting on navigation puts it back.
    window.addEventListener("location-changed", () => setTimeout(paint, 400));
    window.addEventListener("popstate", () => setTimeout(paint, 400));
  }

  window.jarvisSky = {
    repaint: paint,
    debug: () => ({ ...state, ageSeconds: Math.round((Date.now() - state.painted) / 1000) }),
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
