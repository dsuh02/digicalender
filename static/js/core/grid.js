/* The grid: a cell coordinate system with touch drag and resize.
 *
 * A page declares cols x rows (48 x 32 by default — deliberately fine-grained,
 * so a widget can be nudged by a fraction of the screen rather than snapping to
 * chunky thirds). Widgets store x/y/w/h in cells; pixels only ever exist inside
 * this module.
 *
 * Placement is free-form with overlap rejected rather than auto-packed. On a
 * wall display you arrange things once and want them to stay exactly where you
 * put them — a layout that reflows because a neighbour grew is infuriating on a
 * screen you look at every day.
 */

import { clamp, el } from './util.js';

const MIN_W = 3;
const MIN_H = 3;

export class Grid {
  /**
   * @param {HTMLElement} host      container the cells are laid out in
   * @param {object} page           {cols, rows}
   * @param {object} handlers       {onChange(list), onEdit(widget), onDelete(widget)}
   */
  constructor(host, page, handlers = {}) {
    this.host = host;
    this.cols = page.cols || 48;
    this.rows = page.rows || 32;
    this.handlers = handlers;
    this.items = new Map();   // id -> {widget, node}
    this.editing = false;

    host.classList.add('grid');
    host.style.setProperty('--cols', this.cols);
    host.style.setProperty('--rows', this.rows);
  }

  cellSize() {
    const r = this.host.getBoundingClientRect();
    return { w: r.width / this.cols, h: r.height / this.rows, rect: r };
  }

  add(widget, contentNode) {
    const node = el('section.w', { dataset: { id: widget.id, type: widget.type } });
    node.append(
      el('div.w-body', {}, [contentNode]),
      // The grip is a sibling of the chrome, not a child: nested inside it, an
      // absolutely-positioned grip covers the gear and delete buttons and eats
      // their taps.
      el('div.w-grip', { title: 'Drag to move' }),
      el('div.w-chrome', {}, [
        el('button.w-btn', {
          title: 'Options', 'aria-label': 'Widget options',
          onclick: e => { e.stopPropagation(); this.handlers.onEdit?.(widget); },
        }, ['⚙']),
        el('button.w-btn.w-del', {
          title: 'Remove', 'aria-label': 'Remove widget',
          onclick: e => { e.stopPropagation(); this.handlers.onDelete?.(widget); },
        }, ['✕']),
      ]),
      el('div.w-resize', { title: 'Drag to resize' }),
    );
    this.place(node, widget);
    this.host.append(node);
    this.items.set(widget.id, { widget, node });
    this.bindDrag(node, widget);
    return node;
  }

  place(node, w) {
    node.style.gridColumn = `${w.x + 1} / span ${w.w}`;
    node.style.gridRow = `${w.y + 1} / span ${w.h}`;
  }

  remove(id) {
    const it = this.items.get(id);
    if (it) { it.node.remove(); this.items.delete(id); }
  }

  setEditing(on) {
    this.editing = on;
    this.host.classList.toggle('editing', on);
  }

  /** Does this rect collide with anything other than `skipId`? */
  collides(rect, skipId) {
    for (const [id, { widget }] of this.items) {
      if (id === skipId) continue;
      if (rect.x < widget.x + widget.w && rect.x + rect.w > widget.x &&
          rect.y < widget.y + widget.h && rect.y + rect.h > widget.y) return true;
    }
    return false;
  }

  /**
   * First free slot for a widget, scanning top-left to bottom-right.
   *
   * Tries the requested size, then progressively smaller ones down to the
   * widget's minimum, because a full page should still accept a small widget.
   * Returns null when genuinely nothing fits — the caller decides what to tell
   * the user. Never silently returns an overlapping position: free placement
   * only works if the no-overlap rule holds everywhere, and a palette that
   * quietly stacks widgets on top of each other breaks it.
   */
  findSlot(w, h, min = {}) {
    const minW = Math.max(MIN_W, min.w || MIN_W);
    const minH = Math.max(MIN_H, min.h || MIN_H);
    const sizes = [];
    for (const f of [1, 0.75, 0.5]) {
      const tw = Math.max(minW, Math.round(w * f));
      const th = Math.max(minH, Math.round(h * f));
      if (!sizes.some(s => s[0] === tw && s[1] === th)) sizes.push([tw, th]);
    }
    if (!sizes.some(s => s[0] === minW && s[1] === minH)) sizes.push([minW, minH]);

    for (const [tw, th] of sizes) {
      for (let y = 0; y <= this.rows - th; y++) {
        for (let x = 0; x <= this.cols - tw; x++) {
          if (!this.collides({ x, y, w: tw, h: th })) return { x, y, w: tw, h: th };
        }
      }
    }
    return null;
  }

  // ------------------------------------------------------------- gestures

  bindDrag(node, widget) {
    const grip = node.querySelector('.w-grip');
    const handle = node.querySelector('.w-resize');
    this._gesture(grip, node, widget, 'move');
    this._gesture(handle, node, widget, 'resize');
  }

  _gesture(target, node, widget, mode) {
    let start = null;
    let ghost = null;

    const down = (e) => {
      if (!this.editing) return;
      e.preventDefault();
      e.stopPropagation();
      target.setPointerCapture(e.pointerId);
      const { w: cw, h: ch } = this.cellSize();
      start = { px: e.clientX, py: e.clientY, cw, ch, ...widget };
      ghost = el('div.w-ghost');
      this.place(ghost, widget);
      this.host.append(ghost);
      node.classList.add('dragging');
    };

    const move = (e) => {
      if (!start) return;
      const dx = Math.round((e.clientX - start.px) / start.cw);
      const dy = Math.round((e.clientY - start.py) / start.ch);
      let next;
      if (mode === 'move') {
        next = {
          x: clamp(start.x + dx, 0, this.cols - start.w),
          y: clamp(start.y + dy, 0, this.rows - start.h),
          w: start.w, h: start.h,
        };
      } else {
        next = {
          x: start.x, y: start.y,
          w: clamp(start.w + dx, MIN_W, this.cols - start.x),
          h: clamp(start.h + dy, MIN_H, this.rows - start.y),
        };
      }
      ghost.dataset.bad = this.collides(next, widget.id) ? '1' : '';
      this.place(ghost, next);
      ghost._next = next;
    };

    const up = () => {
      if (!start) return;
      const next = ghost?._next;
      node.classList.remove('dragging');
      // Reject overlapping drops rather than shuffling neighbours: on a wall
      // display, things must stay where you put them.
      if (next && !this.collides(next, widget.id)) {
        Object.assign(widget, next);
        this.place(node, widget);
        this.handlers.onChange?.(this.layout());
      }
      ghost?.remove();
      ghost = null;
      start = null;
    };

    target.addEventListener('pointerdown', down);
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
    target.addEventListener('pointercancel', up);
  }

  layout() {
    return [...this.items.values()].map(({ widget: w }) =>
      ({ id: w.id, x: w.x, y: w.y, w: w.w, h: w.h }));
  }
}
