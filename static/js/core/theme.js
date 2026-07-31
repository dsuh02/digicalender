/* Theme engine.
 *
 * The whole palette is generated from six numbers, so it is impossible to
 * produce an incoherent scheme by fiddling with it:
 *
 *   primary / secondary / tertiary   hue angles (0-359)
 *   intensity                        how saturated those three are
 *   brightness                       how dark the background sits
 *   tint                             hue of the neutrals (greys are never
 *                                    truly grey in a good dark UI)
 *
 * Saturation and lightness for the accents are fixed at values tuned for a
 * dark panel, so a hue slider cannot make something garish or unreadable —
 * `intensity` is the single knob between mature and vivid.
 *
 * Roles, and what each is allowed to touch:
 *   PRIMARY    interaction and selection — active tab, primary button, today,
 *              toggles, checkboxes, focus, drag ghost
 *   SECONDARY  device and home state — an "on" tile's icon and dot, scene icons
 *   TERTIARY   informational accents — reminders, precipitation, relative times
 *
 * Neutrals (background, surfaces, lines, text) are all derived from
 * `brightness` + `tint`, so one slider re-tones the entire chrome.
 */

/* ------------------------------------------------------------ colour maths */

const clamp = (n, lo, hi) => Math.max(lo, Math.min(n, hi));

export function hslToHex(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = clamp(s, 0, 100) / 100;
  l = clamp(l, 0, 100) / 100;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const seg = [[c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x]][Math.floor(h / 60) % 6];
  return '#' + seg.map(v => Math.round((v + m) * 255).toString(16).padStart(2, '0')).join('');
}

export function hexToRgb(hex) {
  const h = String(hex).replace('#', '');
  const s = h.length === 3 ? h.split('').map(c => c + c).join('') : h;
  const n = parseInt(s, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

export function rgbToHex({ r, g, b }) {
  return '#' + [r, g, b].map(v => clamp(Math.round(v), 0, 255).toString(16).padStart(2, '0')).join('');
}

/** Blend two hex colours. amount = share of `a`. */
export function mix(a, b, amount) {
  const A = hexToRgb(a), B = hexToRgb(b);
  return rgbToHex({
    r: A.r * amount + B.r * (1 - amount),
    g: A.g * amount + B.g * (1 - amount),
    b: A.b * amount + B.b * (1 - amount),
  });
}

/** Black or white text, whichever is readable on `hex` (WCAG relative luminance). */
export function inkFor(hex) {
  const { r, g, b } = hexToRgb(hex);
  const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const L = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  return L > 0.42 ? '#0b0d10' : '#ffffff';
}

/* ------------------------------------------------------------------ themes */

export const DEFAULT_THEME = {
  primary: 231,      // muted indigo
  secondary: 190,    // teal
  tertiary: 38,      // amber
  intensity: 77,     // 0 = monochrome, 100 = vivid
  brightness: 30,    // 0 = near black, 100 = light charcoal
  tint: 220,         // hue of the neutrals — cool by default
};

export const PRESETS = [
  { name: 'Slate',    theme: { primary: 231, secondary: 190, tertiary: 38,  intensity: 77, brightness: 30, tint: 220 } },
  { name: 'Graphite', theme: { primary: 220, secondary: 220, tertiary: 220, intensity: 18, brightness: 26, tint: 230 } },
  { name: 'Nordic',   theme: { primary: 205, secondary: 175, tertiary: 32,  intensity: 62, brightness: 34, tint: 210 } },
  { name: 'Ember',    theme: { primary: 18,  secondary: 42,  tertiary: 200, intensity: 74, brightness: 26, tint: 25  } },
  { name: 'Moss',     theme: { primary: 145, secondary: 95,  tertiary: 40,  intensity: 60, brightness: 28, tint: 140 } },
  { name: 'Orchid',   theme: { primary: 285, secondary: 320, tertiary: 195, intensity: 70, brightness: 30, tint: 270 } },
  { name: 'Paper',    theme: { primary: 231, secondary: 190, tertiary: 38,  intensity: 55, brightness: 88, tint: 220 } },
];

export function normalize(t = {}) {
  const d = DEFAULT_THEME;
  return {
    primary: clamp(Number(t.primary ?? d.primary), 0, 359),
    secondary: clamp(Number(t.secondary ?? d.secondary), 0, 359),
    tertiary: clamp(Number(t.tertiary ?? d.tertiary), 0, 359),
    intensity: clamp(Number(t.intensity ?? d.intensity), 0, 100),
    brightness: clamp(Number(t.brightness ?? d.brightness), 0, 100),
    tint: clamp(Number(t.tint ?? d.tint), 0, 359),
  };
}

/**
 * Turn the six numbers into every CSS variable the stylesheets consume.
 *
 * Above brightness ~62 the scheme flips to a light one: surfaces get darker
 * than the page instead of lighter, and text inverts. Without that, dragging
 * the brightness slider up produces white text on near-white panels.
 */
export function resolve(theme) {
  const t = normalize(theme);
  const light = t.brightness > 62;

  // Accents: hue is yours, saturation follows intensity, lightness is fixed at
  // a value that stays readable on either polarity.
  const sat = t.intensity * 0.7;
  const accentL = light ? 42 : 60;
  const accent = h => hslToHex(h, sat, accentL);

  const primary = accent(t.primary);
  const secondary = accent(t.secondary);
  const tertiary = accent(t.tertiary);

  // Neutrals: a single lightness ramp, tinted by `tint`. Saturation is kept
  // very low — enough to avoid dead grey, not enough to read as coloured.
  const nsat = light ? 12 : 10;
  const baseL = light ? 96 - (100 - t.brightness) * 0.12 : 4 + t.brightness * 0.10;
  const step = light ? -1 : 1;
  const N = dl => hslToHex(t.tint, nsat, clamp(baseL + dl * step, 2, 99));

  const bg = N(0);
  const surface = N(2.6);
  const surface2 = N(5.2);
  const surface3 = N(8.4);
  const line = N(7.5);
  const lineStrong = N(12);

  const text = light ? hslToHex(t.tint, 18, 14) : hslToHex(t.tint, 14, 92);
  const text2 = light ? hslToHex(t.tint, 12, 38) : hslToHex(t.tint, 10, 68);
  const text3 = light ? hslToHex(t.tint, 10, 54) : hslToHex(t.tint, 8, 46);

  return {
    '--bg': bg,
    '--surface': surface,
    '--surface-2': surface2,
    '--surface-3': surface3,
    '--line': line,
    '--line-strong': lineStrong,

    '--text': text,
    '--text-2': text2,
    '--text-3': text3,

    '--primary': primary,
    '--primary-ink': inkFor(primary),
    '--primary-soft': mix(primary, surface, 0.14),
    '--primary-line': mix(primary, line, 0.45),

    '--secondary': secondary,
    '--secondary-ink': inkFor(secondary),
    '--secondary-soft': mix(secondary, surface, 0.14),

    '--tertiary': tertiary,
    '--tertiary-ink': inkFor(tertiary),
    '--tertiary-soft': mix(tertiary, surface, 0.14),

    /* Status colours keep fixed hues — a warning must not become "whatever hue
       the user picked" — but follow the scheme's saturation and polarity. */
    '--danger': hslToHex(353, Math.max(28, sat * 0.8), accentL),
    '--warn': hslToHex(35, Math.max(30, sat * 0.85), accentL),
    '--good': hslToHex(140, Math.max(24, sat * 0.7), accentL),

    /* Event colours: six evenly spread hues locked to the scheme's intensity,
       so a calendar never clashes with the chrome around it. */
    '--c-1': accent(t.primary),
    '--c-2': accent(t.primary + 95),
    '--c-3': accent(t.primary + 55),
    '--c-4': accent(t.primary + 170),
    '--c-5': accent(t.primary + 210),
    '--c-6': accent(t.primary + 140),

    '--scheme': light ? 'light' : 'dark',
  };
}

/** Six swatches offered in the event/scene colour picker, for the live theme. */
export function eventPalette(theme) {
  const vars = resolve(theme);
  return [
    { name: 'primary', value: vars['--c-1'] },
    { name: 'green', value: vars['--c-2'] },
    { name: 'violet', value: vars['--c-3'] },
    { name: 'amber', value: vars['--c-4'] },
    { name: 'rose', value: vars['--c-5'] },
    { name: 'teal', value: vars['--c-6'] },
  ];
}

let current = { ...DEFAULT_THEME };

export function getTheme() {
  return { ...current };
}

export function apply(theme) {
  current = normalize(theme);
  const vars = resolve(current);
  const root = document.documentElement;
  for (const [k, v] of Object.entries(vars)) {
    if (k === '--scheme') root.dataset.scheme = v;
    else root.style.setProperty(k, v);
  }
  root.style.colorScheme = vars['--scheme'];
  return current;
}
