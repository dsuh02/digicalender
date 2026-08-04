/* Content scaling for widgets.
 *
 * The rule this enforces: **a widget renders all of its content at every size.**
 * It may render it small — that is the user's decision to make, not the app's —
 * but it never drops a label, an axis, or a chart for being cramped. An earlier
 * version hid content below fixed breakpoints, which quietly overrode the size
 * the user had chosen and left boxes looking broken rather than dense.
 *
 * One mechanism does all of it: `zoom` on a wrapper inside the widget body.
 *
 * Why `zoom` and not `transform: scale()` — transform paints at the old size and
 * squashes the result, so a scaled-down widget still reserves its full layout
 * box and overflows its neighbours. `zoom` participates in layout: the content
 * re-flows into the larger logical box and is painted smaller. Text stays
 * crisp, flex and grid recompute, and SVG charts measure the logical size and
 * draw at full fidelity.
 *
 * Why not container-query units for the type — cq units resolve against the
 * nearest container ancestor, which sits INSIDE the zoom. Scaling down enlarges
 * the logical box, cq-based type grows to match, and the zoom shrinks it back:
 * the two cancel and the slider does nothing. One scaling mechanism, not two
 * fighting. Sizes inside a widget are therefore plain pixels.
 *
 * Two modes:
 *   auto   — the app picks a zoom from the box, relative to the size the widget
 *            was designed for. Bigger box, roomier content; smaller box,
 *            tighter, continuously and with no cliffs.
 *   manual — the user pins it with a slider, and everything is forced to fit at
 *            that size.
 */

const MIN_ZOOM = 0.2;      // far below legible; a floor only against 0 and NaN
const MAX_ZOOM = 2.5;

/** Reference box a widget's content was designed to look right in. */
function referenceBox(def, cell) {
  const w = (def?.defaultSize?.w || 14) * cell.w;
  const h = (def?.defaultSize?.h || 10) * cell.h;
  return { w: Math.max(80, w), h: Math.max(60, h) };
}

/**
 * Auto zoom from the measured box.
 *
 * The LIMITING dimension wins: a box that is wide but short has to shrink to
 * the short side, or the content overflows vertically and gets clipped — which
 * is the hiding this whole module exists to prevent.
 *
 * Scaling up is deliberately gentler than scaling down (sqrt, not linear). A
 * widget stretched to a quarter of the wall should breathe, not turn into three
 * enormous words.
 */
export function autoZoom(w, h, ref) {
  if (!(w > 0) || !(h > 0)) return 1;
  const raw = Math.min(w / ref.w, h / ref.h);
  const z = raw >= 1 ? Math.sqrt(raw) : raw;
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
}

/** Logical size, as a percentage, that paints flush at this zoom. */
export function inverseSize(z) {
  return 100 / clampZoom(z);
}

export function clampZoom(z) {
  const n = Number(z);
  if (!Number.isFinite(n) || n <= 0) return 1;
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, n));
}

/**
 * Keep `inner`'s zoom in step with `body`'s size and the widget's settings.
 *
 * Returns { update(settings), destroy() }. `update` is called when the user
 * saves options, so a change to the slider takes effect without a remount.
 */
export function contentScale(body, inner, def, cellSize) {
  let settings = {};
  let frame = null;
  let lastKey = '';

  const apply = () => {
    frame = null;
    const auto = settings.autoScale !== false;
    let z;
    if (auto) {
      const w = body.clientWidth, h = body.clientHeight;
      if (!(w > 1 && h > 1)) return;          // not laid out yet
      z = autoZoom(w, h, referenceBox(def, cellSize()));
    } else {
      z = clampZoom((Number(settings.contentScale) || 100) / 100);
    }
    const key = z.toFixed(3);
    if (key === lastKey) return;
    lastKey = key;
    // Rounded to 3dp so a pixel of resize jitter cannot thrash the layout.
    inner.style.zoom = key;
    // The inverse size is not optional. A percentage width resolves against the
    // containing block in the PARENT's coordinates and is only then scaled by
    // the zoom, so `width: 100%` at zoom 0.5 paints half the box and leaves the
    // rest empty. Enlarging the logical box by 1/z makes the painted result
    // land exactly on the parent's edges, which is what turns the zoom into
    // "more content fits" instead of "content moves into a corner".
    const inv = (100 / z).toFixed(4);
    inner.style.width = `${inv}%`;
    inner.style.height = `${inv}%`;
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
      lastKey = '';                            // force a re-apply
      schedule();
    },
    destroy() {
      ro.disconnect();
      if (frame) cancelAnimationFrame(frame);
    },
  };
}
