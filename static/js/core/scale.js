/* Content scaling for widgets.
 *
 * The rule this enforces: **a widget renders all of its content at every size.**
 * It may render it small — that is the user's decision to make, not the app's —
 * but it never drops a label, an axis, or a chart for being cramped.
 *
 * The mechanism is one CSS custom property, `--ui-scale`, which every size
 * inside a widget is multiplied by. Charts read the same number and scale their
 * type and padding to match.
 *
 * **This used to use `zoom`, and that was a mistake worth recording.** The idea
 * was sound — zoom participates in layout, so content reflows into a larger
 * logical box and paints smaller — but it depends on two behaviours that are
 * easy to get backwards and that no amount of reading settles: how percentage
 * lengths resolve inside a zoomed element, and whether clientWidth reports the
 * logical or the painted size. The first was guessed wrong: the code multiplied
 * the box by 1/z believing percentages resolved in the PARENT's space, when
 * Chromium resolves them in the zoomed space. The compensation therefore
 * applied the scale a second time, inverted — asking for 150% made everything
 * smaller and asking for 50% made it bigger.
 *
 * A plain multiplier has no such ambiguity. `calc(14px * var(--ui-scale))` is
 * 21px at 1.5 in every engine, and it is checkable with arithmetic instead of
 * a browser.
 *
 * Two modes:
 *   auto   — the app picks a scale from the box, relative to the size the
 *            widget was designed for. Bigger box, roomier content; smaller box,
 *            tighter, continuously and with no cliffs.
 *   manual — the user pins it with a slider, and everything is forced to that
 *            size.
 */

const MIN_SCALE = 0.2;     // far below comfortable; a floor only against 0/NaN
const MAX_SCALE = 2.5;

/** Reference box a widget's content was designed to look right in. */
function referenceBox(def, cell) {
  const w = (def?.defaultSize?.w || 14) * cell.w;
  const h = (def?.defaultSize?.h || 10) * cell.h;
  return { w: Math.max(80, w), h: Math.max(60, h) };
}

/**
 * Auto scale from the measured box.
 *
 * The LIMITING dimension wins: a box that is wide but short has to shrink to
 * the short side, or the content overflows vertically and gets clipped — which
 * is the hiding this whole module exists to prevent.
 *
 * Scaling up is deliberately gentler than scaling down (sqrt, not linear). A
 * widget stretched to a quarter of the wall should breathe, not turn into three
 * enormous words.
 */
export function autoScaleFor(w, h, ref) {
  if (!(w > 0) || !(h > 0)) return 1;
  const raw = Math.min(w / ref.w, h / ref.h);
  const s = raw >= 1 ? Math.sqrt(raw) : raw;
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, s));
}

export function clampScale(s) {
  const n = Number(s);
  if (!Number.isFinite(n) || n <= 0) return 1;
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, n));
}

/** The scale in force on an element, for code that has to compute in pixels. */
export function scaleOf(node) {
  if (!node) return 1;
  const raw = getComputedStyle(node).getPropertyValue('--ui-scale');
  const n = parseFloat(raw);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

/**
 * Keep `inner`'s --ui-scale in step with `body`'s size and the widget settings.
 *
 * Returns { update(settings), destroy() }. `update` is called when the user
 * saves options, so a change to the slider takes effect without a remount.
 */
export function contentScale(body, inner, def, cellSize) {
  let settings = {};
  let frame = null;
  let last = '';

  const apply = () => {
    frame = null;
    const auto = settings.autoScale !== false;
    let s;
    if (auto) {
      // Measured on the UNSCALED body. Measuring anything the scale affects
      // would feed this its own output and it would never settle.
      const w = body.clientWidth, h = body.clientHeight;
      if (!(w > 1 && h > 1)) return;
      s = autoScaleFor(w, h, referenceBox(def, cellSize()));
    } else {
      s = clampScale((Number(settings.contentScale) || 100) / 100);
    }
    // 3dp, so a pixel of resize jitter cannot thrash every dependent length.
    const key = s.toFixed(3);
    if (key === last) return;
    last = key;
    inner.style.setProperty('--ui-scale', key);
  };

  const schedule = () => {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(apply);
  };

  const ro = new ResizeObserver(schedule);
  ro.observe(body);
  schedule();

  return {
    update(next) {
      settings = next || {};
      last = '';                               // force a re-apply
      schedule();
    },
    destroy() {
      ro.disconnect();
      if (frame) cancelAnimationFrame(frame);
    },
  };
}
