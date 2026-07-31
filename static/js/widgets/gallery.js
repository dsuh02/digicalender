/* Gallery slideshow widget + the fullscreen screensaver.
 *
 * Both run the same engine: current image visible on a black ground, fade the
 * image out (to black), swap the source while dark, fade back in. Next image
 * is preloaded during the visible phase so the dark beat never stalls on the
 * network.
 *
 * The in-box widget always cycles and deliberately has NO say over screen or
 * sleep settings — idle dimming rolls right over it. The fullscreen
 * screensaver (tap the widget) is the opposite: it suppresses idle dimming
 * while it runs, exits on any touch or after 3 hours, and hands the idle
 * clock back untouched — walk away and the panel dims/sleeps right after the
 * show ends, exactly as if the screensaver had never existed.
 */

import { api, bus } from '../core/api.js';
import { toast } from '../core/sheet.js';
import { clear, el } from '../core/util.js';

const FADE_MS = 1800;
const SCREENSAVER_MAX_MS = 3 * 60 * 60 * 1000;   // the spec: 3 hours, fixed

/** The shared fade loop. Returns {stop}. */
function runShow(img, imageIds, intervalMs) {
  let at = 0;
  let timer = null;
  let stopped = false;

  const src = i => api.imageUrl(imageIds[i]);
  img.src = src(0);
  img.style.opacity = '1';

  const preload = i => { const p = new Image(); p.src = src(i); };

  const step = () => {
    if (stopped || imageIds.length < 2) return;
    const next = (at + 1) % imageIds.length;
    preload(next);
    img.style.opacity = '0';                       // fade to black…
    timer = setTimeout(() => {
      if (stopped) return;
      at = next;
      img.src = src(at);
      // Let the new source land before fading back out of black.
      timer = setTimeout(() => {
        if (stopped) return;
        img.style.opacity = '1';
        timer = setTimeout(step, intervalMs);
      }, 180);
    }, FADE_MS);
  };
  timer = setTimeout(step, intervalMs);

  return {
    stop() {
      stopped = true;
      clearTimeout(timer);
    },
  };
}

/* ------------------------------------------------------------- screensaver */

let saver = null;   // {node, show, timer}

export function screensaverActive() {
  return !!saver;
}

export function stopScreensaver() {
  if (!saver) return;
  saver.show.stop();
  clearTimeout(saver.timer);
  saver.node.remove();
  saver = null;
}

export async function startScreensaver(galleryId, { interval = 20, fit = 'contain' } = {}) {
  stopScreensaver();
  let images = [];
  try { images = await api.galleryImages(galleryId); } catch { images = []; }
  if (!images.length) {
    toast('That gallery set has no images yet', true);
    return;
  }

  const img = el('img.ss-img', { alt: '', style: { objectFit: fit } });
  const node = el('div.screensaver', { id: 'screensaver' }, [img]);

  // Any touch ends the show. Swallow it — ending a screensaver is not a
  // command to whatever sat underneath the finger.
  for (const t of ['pointerdown', 'pointerup', 'click']) {
    node.addEventListener(t, e => {
      e.stopPropagation();
      e.preventDefault();
      if (t === 'pointerdown') stopScreensaver();
    });
  }

  document.body.append(node);
  const show = runShow(img, images.map(i => i.id), Math.max(5, interval) * 1000);
  saver = {
    node, show,
    // The 3-hour cap ends the show WITHOUT touching the idle clock — the
    // panel then dims/sleeps on the next tick, as if we were never here.
    timer: setTimeout(stopScreensaver, SCREENSAVER_MAX_MS),
  };
}

/* ------------------------------------------------------------------ widget */

export const GalleryWidget = {
  type: 'gallery', name: 'Gallery slideshow', icon: 'image', category: 'Info',
  defaultSize: { w: 16, h: 12 }, minSize: { w: 4, h: 3 },
  settings: [
    { key: 'galleryId', label: 'Gallery set', type: 'gallery',
      help: 'Create and fill sets under Settings › Galleries' },
    { key: 'interval', label: 'Seconds per image', type: 'slider',
      min: 5, max: 120, default: 20 },
    { key: 'fit', label: 'Fill style', type: 'select', default: 'cover',
      options: [{ value: 'cover', label: 'Fill the box (crop)' },
                { value: 'contain', label: 'Fit whole image' }] },
    { key: 'tapFullscreen', label: 'Tap for fullscreen screensaver', type: 'toggle',
      default: true },
  ],
  render(host, ctx) {
    const body = el('div.gallery-box');
    host.append(body);
    let show = null;

    const load = async () => {
      show?.stop();
      show = null;
      clear(body);
      const gid = ctx.settings.galleryId;
      if (!gid) {
        body.append(el('p.empty-hint', { text: 'Pick a gallery set in this widget’s options' }));
        return;
      }
      let images = [];
      try { images = await api.galleryImages(gid); } catch { images = []; }
      if (!images.length) {
        body.append(el('p.empty-hint', { text: 'This set is empty — add images in Settings › Galleries' }));
        return;
      }
      const img = el('img.gallery-img', { alt: '', style: { objectFit: ctx.settings.fit || 'cover' } });
      body.append(img);
      show = runShow(img, images.map(i => i.id),
                     Math.max(5, Number(ctx.settings.interval || 20)) * 1000);
    };

    body.addEventListener('click', () => {
      if (ctx.settings.tapFullscreen === false || !ctx.settings.galleryId) return;
      startScreensaver(ctx.settings.galleryId, {
        interval: Number(ctx.settings.interval || 20),
        fit: ctx.settings.fit || 'contain',
      });
    });

    const off = bus.on('galleries_changed', load);
    load();
    return { refresh: load, destroy: () => { off(); show?.stop(); } };
  },
};
