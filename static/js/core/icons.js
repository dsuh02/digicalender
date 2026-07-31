/* Inline SVG icons. Inline rather than an icon font or sprite sheet because
 * the panel must render correctly with no network and no external assets. */

const P = {
  calendar: 'M7 2v3M17 2v3M3 9h18M5 5h14a2 2 0 012 2v12a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2z',
  clock: 'M12 7v5l3 2M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  bell: 'M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 01-3.4 0',
  sun: 'M12 3v2M12 19v2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M3 12h2M19 12h2M5.6 18.4L7 17M17 7l1.4-1.4M16 12a4 4 0 11-8 0 4 4 0 018 0z',
  cloud: 'M18 10h-1.3A5 5 0 107 15h11a4 4 0 000-8z',
  'cloud-sun': 'M12 3v1M4.9 5.9l.7.7M3 12h1M17.7 6.6l.7-.7M18 13h-1a4 4 0 10-7.5 2H18a2.5 2.5 0 000-5z',
  rain: 'M18 10h-1.3A5 5 0 107 15h11a4 4 0 000-8zM8 19l-1 2M12 19l-1 2M16 19l-1 2',
  drizzle: 'M18 10h-1.3A5 5 0 107 15h11a4 4 0 000-8zM9 19v1M13 19v1',
  snow: 'M18 10h-1.3A5 5 0 107 15h11a4 4 0 000-8zM8 19h.01M12 20h.01M16 19h.01',
  sleet: 'M18 10h-1.3A5 5 0 107 15h11a4 4 0 000-8zM9 19l-1 2M13 19h.01',
  storm: 'M18 10h-1.3A5 5 0 107 15h11a4 4 0 000-8zM13 15l-3 5h4l-3 4',
  fog: 'M4 15h16M4 19h10M18 10h-1.3A5 5 0 107 13h11a1.5 1.5 0 000-3z',
  plug: 'M9 2v6M15 2v6M6 8h12v3a6 6 0 01-12 0V8zM12 17v5',
  bulb: 'M9 18h6M10 22h4M12 2a7 7 0 00-4 12.7V17h8v-2.3A7 7 0 0012 2z',
  tv: 'M2 7h20v12H2zM8 3l4 4 4-4',
  remote: 'M8 2h8a2 2 0 012 2v16a2 2 0 01-2 2H8a2 2 0 01-2-2V4a2 2 0 012-2zM12 6v2M9 12h.01M12 12h.01M15 12h.01M9 16h.01M12 16h.01M15 16h.01',
  speaker: 'M11 5L6 9H2v6h4l5 4V5zM15.5 8.5a5 5 0 010 7M19 5a9 9 0 010 14',
  play: 'M6 4l14 8-14 8V4z',
  pause: 'M9 4v16M15 4v16',
  rewind: 'M11 5L4 12l7 7V5zM20 5l-7 7 7 7V5z',
  forward: 'M13 5l7 7-7 7V5zM4 5l7 7-7 7V5z',
  back: 'M9 14L4 9l5-5M4 9h11a5 5 0 010 10h-4',
  /* Volume: three glyphs that differ only in the number of arcs, so the row
     reads as one control rather than three unrelated symbols. */
  'volume-low': 'M11 5L6 9H2v6h4l5 4V5z',
  'volume-mute': 'M11 5L6 9H2v6h4l5 4V5zM22 9l-6 6M16 9l6 6',
  'volume-high': 'M11 5L6 9H2v6h4l5 4V5zM15.5 8.5a5 5 0 010 7M19 5a9 9 0 010 14',
  sparkles: 'M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3zM19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z',
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z',
  text: 'M4 6h16M7 12h10M9 18h6',
  plus: 'M12 5v14M5 12h14',
  lock: 'M7 11V7a5 5 0 0110 0v4M5 11h14v10H5z',
  unlock: 'M7 11V7a5 5 0 019.9-1M5 11h14v10H5z',
  /* Ring + spokes rather than a cogged outline: it stays legible at 15px,
     which the toothed version does not. */
  gear: 'M12 15.5a3.5 3.5 0 110-7 3.5 3.5 0 010 7zM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1',
  /* Used for per-widget options. A ring-and-spokes gear at 15px is hard to
     tell from the weather `sun`, and "tune this one thing" is what sliders
     mean anyway — the cog stays for app-wide settings. */
  sliders: 'M3 7h18M3 12h18M3 17h18M9 5v4M16 10v4M6 15v4',
  layers: 'M12 2L2 8l10 6 10-6-10-6zM2 13l10 6 10-6M2 18l10 6 10-6',
  eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7zM12 15a3 3 0 100-6 3 3 0 000 6z',
  'eye-off': 'M3 3l18 18M10.6 5.1A10.9 10.9 0 0112 5c6.5 0 10 7 10 7a17.6 17.6 0 01-3.2 3.9M6.6 6.6C3.8 8.5 2 12 2 12s3.5 7 10 7c1.9 0 3.6-.6 5-1.4M9.9 9.9a3 3 0 104.2 4.2',
  refresh: 'M21 12a9 9 0 11-2.6-6.3M21 3v6h-6',
  link: 'M10 14a5 5 0 007.5.5l3-3a5 5 0 00-7-7l-1.7 1.7M14 10a5 5 0 00-7.5-.5l-3 3a5 5 0 007 7l1.7-1.7',
  check: 'M20 6L9 17l-5-5',
  trash: 'M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6',
  wifi: 'M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0M12 20h.01M2 9a15 15 0 0120 0',
  home: 'M3 10l9-7 9 7v10a2 2 0 01-2 2H5a2 2 0 01-2-2V10z',
  power: 'M12 3v9M18.4 6.6a9 9 0 11-12.8 0',
};

export function icon(name, size = 20, cls = '') {
  const d = P[name] || P.grid;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  if (cls) svg.setAttribute('class', cls);
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}

export const ICON_NAMES = Object.keys(P);
