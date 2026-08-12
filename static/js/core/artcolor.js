/* Average colour of an image, softened into something you can put type on.
 *
 * Spotify's image CDN sends `access-control-allow-origin: *`, so the canvas is
 * not tainted and the pixels can actually be read. That is the whole reason
 * this can happen in the browser instead of needing a server-side image
 * decoder — which, with no third-party packages, would mean hand-rolling a JPEG
 * decoder for the sake of one colour.
 *
 * A raw mean is rarely usable as a surface: album art is often heavily
 * saturated, and the honest average of a neon cover is a neon bar that fights
 * everything on it. So the mean is desaturated and its lightness pulled into a
 * band, which is what "softer" means here — the colour still reads as belonging
 * to the artwork, but it behaves like a surface rather than a highlight.
 */

const CACHE = new Map();       // url -> {bg, ink}
const SAMPLE = 32;             // 32x32 is far more than enough for a mean

function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) h = ((b - r) / d + 2) / 6;
  else h = ((r - g) / d + 4) / 6;
  return [h, s, l];
}

function hslToRgb(h, s, l) {
  if (s === 0) { const v = Math.round(l * 255); return [v, v, v]; }
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const hue = (t) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  return [hue(h + 1 / 3), hue(h), hue(h - 1 / 3)].map(v => Math.round(v * 255));
}

/** WCAG relative luminance — the thing that actually decides black or white. */
export function luminance(r, g, b) {
  const f = (v) => {
    v /= 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

/** Whichever of black or white contrasts better against this background. */
export function inkFor(r, g, b) {
  const L = luminance(r, g, b);
  const withWhite = 1.05 / (L + 0.05);
  const withBlack = (L + 0.05) / 0.05;
  return withBlack >= withWhite ? '#000' : '#fff';
}

function soften([r, g, b]) {
  const [h, s, l] = rgbToHsl(r, g, b);
  // Saturation down so it reads as a surface; lightness pulled off both
  // extremes so there is always somewhere for type to sit.
  const s2 = Math.min(s * 0.55, 0.42);
  const l2 = Math.max(0.20, Math.min(0.78, l));
  return hslToRgb(h, s2, l2);
}

/**
 * Resolve an image to `{ bg, ink }` — a softened average and the text colour
 * that contrasts with it. Never rejects: art that will not load returns null
 * and the caller keeps its normal styling.
 */
export function artColor(url) {
  if (!url) return Promise.resolve(null);
  if (CACHE.has(url)) return Promise.resolve(CACHE.get(url));

  return new Promise((resolve) => {
    const img = new Image();
    // Without this the canvas is tainted and getImageData throws, even though
    // the CDN permits the read.
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      try {
        const c = document.createElement('canvas');
        c.width = SAMPLE; c.height = SAMPLE;
        const g = c.getContext('2d', { willReadFrequently: true });
        g.drawImage(img, 0, 0, SAMPLE, SAMPLE);
        const px = g.getImageData(0, 0, SAMPLE, SAMPLE).data;
        let r = 0, gg = 0, b = 0, n = 0;
        for (let i = 0; i < px.length; i += 4) {
          // Skip near-transparent pixels; they average toward black otherwise.
          if (px[i + 3] < 16) continue;
          r += px[i]; gg += px[i + 1]; b += px[i + 2]; n++;
        }
        if (!n) { resolve(null); return; }
        const soft = soften([r / n, gg / n, b / n]);
        const out = {
          bg: `rgb(${soft[0]}, ${soft[1]}, ${soft[2]})`,
          ink: inkFor(soft[0], soft[1], soft[2]),
          rgb: soft,
        };
        CACHE.set(url, out);
        resolve(out);
      } catch {
        resolve(null);          // tainted canvas, or no 2d context
      }
    };
    img.onerror = () => resolve(null);
    img.src = url;
  });
}
