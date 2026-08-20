/* Spotify widgets: what is playing, what is next, and what you actually listen to.
 *
 * Progress is INTERPOLATED between fetches rather than polled for. Spotify rate
 * limits, and a bar that only moves every eight seconds looks broken — so the
 * position is refetched on a slow clock and advanced locally in between, using
 * the timestamp of the last fetch. Pausing stops the local advance too.
 *
 * Album art is the one place this panel uses a big image. Everything else here
 * is type on a flat surface, so the art carries the whole widget and is allowed
 * to bleed to the edges.
 */

import { api, bus } from '../core/api.js';
import { artColor } from '../core/artcolor.js';
import { autoSize } from '../core/charts.js';
import { icon } from '../core/icons.js';
import { toast } from '../core/sheet.js';
import { clear, el } from '../core/util.js';

const REFETCH_MS = 10000;   // the server caches upstream; this is just liveness
const TICK_MS = 250;        // smooth enough for a progress bar
const ARM_MS = 20000;       // how long scrubbing stays available after a tap

function mmss(ms) {
  const t = Math.max(0, Math.round((ms || 0) / 1000));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

/** Largest artwork at or above `want` px, else the biggest there is. */
function artUrl(track, want = 640) {
  const arts = (track && track.art) || [];
  if (!arts.length) return '';
  const big = arts.filter(a => (a.w || 0) >= want).sort((a, b) => a.w - b.w)[0];
  return (big || arts[0]).url || '';
}

/* --------------------------------------------------------------- now playing */

export const NowPlayingWidget = {
  type: 'now_playing', name: 'Now playing', icon: 'speaker', category: 'Home',
  defaultSize: { w: 16, h: 14 }, minSize: { w: 5, h: 4 },
  settings: [
    { key: 'showArt', label: 'Album art background', type: 'toggle', default: true },
    { key: 'showControls', label: 'Playback controls', type: 'toggle', default: true },
    { key: 'showCode', label: 'Scannable code', type: 'toggle', default: false,
      help: 'A Spotify Code for what is playing — scan it with a phone to open it there.' },
  ],

  render(host, ctx) {
    const body = el('div.np');
    host.append(body);

    let data = null;
    let fetchedAt = 0;          // performance.now() of the last successful load
    let poll = null;
    let tick = null;
    let busy = false;

    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        data = await api.spotifyNow();
        fetchedAt = performance.now();
      } catch (e) {
        data = { playing: false, track: null, error: e.message };
      }
      busy = false;
      draw();
    };

    /** Where the track is NOW, advancing locally since the last fetch. */
    const position = () => {
      if (!data || !data.track) return 0;
      const base = data.progress_ms || 0;
      if (!data.playing) return base;
      return Math.min(data.track.duration_ms || 0,
                      base + (performance.now() - fetchedAt));
    };

    const send = async (command, extra) => {
      try {
        await api.spotifyControl(command, extra);
        // Spotify needs a moment before the player reflects the change.
        setTimeout(load, 350);
        return true;
      } catch (e) {
        toast(e.message, true);
        // Re-read on ANY refused command. The UI has already moved
        // optimistically — the scrub bar in particular — and a refusal means
        // that optimism was wrong. The DJ answers 403 "Restriction violated"
        // to a seek, so this is a real path, not a theoretical one.
        load();
        return false;
      }
    };

    let bar = null;
    let knob = null;
    let elapsed = null;
    let track = null;
    let tone = null;            // {bg, ink, isLight} from the artwork
    // Armed = the user has tapped once. Seeking a wall panel by brushing past
    // it would be miserable, so scrubbing is deliberately behind one tap, and
    // it disarms itself rather than staying live forever.
    let armed = false;
    let disarmTimer = null;

    const setArmed = (on) => {
      armed = on;
      body.classList.toggle('armed', on);
      clearTimeout(disarmTimer);
      if (on) disarmTimer = setTimeout(() => setArmed(false), ARM_MS);
      applyTone();
    };

    /** Footer colour: the artwork's tint normally, flat black or white armed. */
    const applyTone = () => {
      const footer = body.querySelector('.np-footer');
      if (!footer || !tone) return;
      if (armed) {
        // Which flat colour is decided by the SAME light/dark call already made
        // for the tint, so the two can never disagree.
        const flat = tone.isLight ? '#fff' : '#000';
        footer.style.background = flat;
        footer.style.color = tone.isLight ? '#000' : '#fff';
        footer.classList.toggle('on-light', tone.isLight);
      } else {
        footer.style.background = tone.bg;
        footer.style.color = tone.ink;
        footer.classList.toggle('on-light', tone.ink === '#000');
      }
      const code = body.querySelector('.np-code');
      if (code) code.src = codeSrc();
    };

    const codeSrc = () => {
      if (!data || !data.context) return '';
      const uri = data.context.uri || (data.track && data.track.uri) || '';
      if (!uri) return '';
      const dark = armed ? !tone?.isLight : (tone ? tone.ink === '#fff' : true);
      return `/api/spotify/code.svg?uri=${encodeURIComponent(uri)}&ink=${dark ? 'white' : 'black'}`;
    };

    const draw = () => {
      clear(body);
      if (!data || !data.track) {
        body.append(el('div.empty-hint', {}, [
          el('div', { text: data && data.error ? data.error : 'Nothing playing' }),
          data && data.error ? null
            : el('div.field-help', { text: 'Start something on any device and it appears here.' }),
        ]));
        return;
      }

      const t = data.track;
      const art = artUrl(t, 640);
      if (ctx.settings.showArt !== false && art) {
        body.append(el('div.np-art', { style: { backgroundImage: `url("${art}")` } }));
        body.classList.add('has-art');
      } else {
        body.classList.remove('has-art');
      }

      const info = el('div.np-info', {}, [
        el('div.np-title', { text: t.name }),
        el('div.np-artist', { text: t.artists.join(', ') }),
        el('div.np-album', {
          text: [t.album, (t.release_date || '').slice(0, 4)].filter(Boolean).join(' · '),
        }),
      ]);

      bar = el('div.np-bar-fill');
      elapsed = el('span.np-time');
      knob = el('div.np-bar-knob');
      track = el('div.np-bar', {}, [bar, knob]);

      // Scrubbing, live only while armed. Pointer capture so a finger that
      // slides off the bar keeps controlling it, and the seek is sent on
      // RELEASE rather than continuously — one request, not thirty.
      let scrubbing = false;
      const fractionAt = (clientX) => {
        const r = track.getBoundingClientRect();
        return r.width ? Math.max(0, Math.min(1, (clientX - r.left) / r.width)) : 0;
      };
      const preview = (f) => {
        setFill(f);
        if (elapsed) elapsed.textContent = mmss(f * (t.duration_ms || 0));
      };
      track.addEventListener('pointerdown', (e) => {
        if (!armed) return;               // a tap elsewhere arms it first
        e.stopPropagation();
        scrubbing = true;
        track.setPointerCapture(e.pointerId);
        preview(fractionAt(e.clientX));
      });
      track.addEventListener('pointermove', (e) => {
        if (!scrubbing) return;
        e.stopPropagation();
        preview(fractionAt(e.clientX));
      });
      const endScrub = (e) => {
        if (!scrubbing) return;
        scrubbing = false;
        e.stopPropagation();
        const ms = Math.round(fractionAt(e.clientX) * (t.duration_ms || 0));
        // Assume it worked so the bar does not snap backwards for the half
        // second before Spotify reports the new position.
        data.progress_ms = ms;
        fetchedAt = performance.now();
        // Some contexts refuse seeking outright — the DJ answers 403
        // "Restriction violated". Re-read on failure so the bar snaps back to
        // where the player really is instead of sitting on a lie for a poll.
        send('seek', { position_ms: ms });
        setArmed(true);                   // a scrub restarts the disarm clock
      };
      track.addEventListener('pointerup', endScrub);
      track.addEventListener('pointercancel', () => { scrubbing = false; });

      info.append(
        track,
        el('div.np-times', {}, [
          elapsed,
          el('span.np-time', { text: mmss(t.duration_ms) }),
        ]),
      );

      const dev = data.device || {};
      if (dev.name) {
        info.append(el('div.np-device', {
          text: `${dev.name}${dev.volume != null ? ` · ${dev.volume}%` : ''}`,
        }));
      }

      body.append(info);

      /* The footer: one flat bar tinted from the artwork's average colour, with
         the controls and the scannable code sitting on it. Its ink is black or
         white by contrast ratio rather than by taste, so a pale cover and a
         dark one are both readable without anyone choosing. */
      const footer = el('div.np-footer');

      if (ctx.settings.showControls !== false) {
        footer.append(el('div.np-controls', {}, [
          el('button.np-btn', {
            type: 'button', 'aria-label': 'Previous',
            onclick: e => { e.stopPropagation(); send('previous'); },
          }, [icon('rewind', 20)]),
          el('button.np-btn.np-play', {
            type: 'button', 'aria-label': data.playing ? 'Pause' : 'Play',
            onclick: e => { e.stopPropagation(); send(data.playing ? 'pause' : 'resume'); },
          }, [icon(data.playing ? 'pause' : 'play', 24)]),
          el('button.np-btn', {
            type: 'button', 'aria-label': 'Next',
            onclick: e => { e.stopPropagation(); send('next'); },
          }, [icon('forward', 20)]),
          el('button.np-btn' + (data.shuffle ? '.on' : ''), {
            type: 'button', 'aria-label': 'Shuffle',
            onclick: e => { e.stopPropagation(); send('shuffle', { on: !data.shuffle }); },
          }, [icon('shuffle', 18)]),
        ]));
      }

      // A Spotify Code, not a Jam link — Jam has no API at all, so this is the
      // closest thing to "share what is on": scan it and it opens on a phone.
      if (ctx.settings.showCode) {
        const src = codeSrc();
        if (src) footer.append(el('img.np-code', { src, alt: 'Scan to open in Spotify' }));
      }

      if (footer.childNodes.length) body.append(footer);

      // Tint after layout. The image may already be cached, in which case this
      // resolves in the same frame; if it is a fresh cover the bar starts
      // neutral and settles, which is far better than blocking the render on a
      // network image.
      if (art) {
        artColor(art).then((c) => {
          if (!c || !footer.isConnected) return;
          tone = c;
          applyTone();
        });
      } else {
        tone = null;
      }

      paintProgress();
    };

    /** The fill and the knob are one position, so they move together. Keeping
     *  them as two separate writes is how the knob stayed at zero. */
    const setFill = (f) => {
      const pct = Math.max(0, Math.min(1, f)) * 100;
      if (bar) bar.style.width = `${pct}%`;
      if (knob) knob.style.left = `${pct}%`;
    };

    const paintProgress = () => {
      if (!bar || !data || !data.track) return;
      const pos = position();
      const pct = data.track.duration_ms
        ? Math.max(0, Math.min(100, (pos / data.track.duration_ms) * 100)) : 0;
      setFill(pct / 100);
      if (elapsed) elapsed.textContent = mmss(pos);
      // The local clock will drift past the end; refetch rather than sit there.
      if (data.playing && data.track.duration_ms
          && pos >= data.track.duration_ms - 400) load();
    };

    // A single tap anywhere in the box arms it: the footer goes flat and the
    // progress bar becomes scrubbable. Tapping again puts it back.
    body.addEventListener('click', () => setArmed(!armed));

    load();
    poll = setInterval(load, REFETCH_MS);
    tick = setInterval(paintProgress, TICK_MS);
    const off = bus.on('spotify_changed', load);

    return {
      refresh: load,
      destroy: () => {
        off(); clearInterval(poll); clearInterval(tick); clearTimeout(disarmTimer);
      },
    };
  },
};

/* ------------------------------------------------------------------ up next */

/**
 * How many slots to spend on each side of the current track.
 *
 * The rule: centre the current track when there is enough on both sides to
 * fill the box, otherwise use whatever there is. So a full history and an empty
 * queue puts "now" at the END (bottom, or right) with the past above it, and an
 * empty history with a full queue puts it at the START — in both cases the box
 * stays full rather than half-empty around a centred item.
 */
export function timelineWindow(nPast, nNext, capacity) {
  if (capacity <= 0) return { before: 0, after: 0 };
  if (capacity === 1) return { before: 0, after: 0 };
  const slots = capacity - 1;                    // current occupies one
  let before = Math.min(nPast, Math.floor(slots / 2));
  let after = Math.min(nNext, slots - before);
  // Whatever the other side could not use comes back to this one.
  before = Math.min(nPast, slots - after);
  return { before, after };
}

/**
 * A tap on a track plays it.
 *
 * Bound on pointerup with a movement guard rather than on `click`, because this
 * widget lives on a page you SWIPE between: a drag that happens to start on a
 * row must scroll the page, not start a song. Eight pixels is the same
 * threshold the grid uses to tell a tap from a drag.
 */
function bindTap(node, act, liveContext) {
  let from = null;
  node.addEventListener('pointerdown', (e) => { from = [e.clientX, e.clientY]; });
  node.addEventListener('pointercancel', () => { from = null; });
  node.addEventListener('pointerup', async (e) => {
    if (!from) return;
    const moved = Math.hypot(e.clientX - from[0], e.clientY - from[1]);
    from = null;
    if (moved > 8) return;
    e.stopPropagation();
    node.classList.add('starting');
    try {
      if (act.kind === 'skip') {
        await api.spotifyControl('skip_to', { steps: act.steps });
      } else {
        // The context goes with it where we know one, so playing a song from a
        // playlist keeps playing the playlist afterwards instead of stopping.
        // A history entry that recorded no context borrows the live one.
        await api.spotifyControl('play_track', {
          uri: act.track.uri,
          context_uri: act.track.context_uri || liveContext || '',
        });
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      node.classList.remove('starting');
    }
  });
}

export const UpNextWidget = {
  type: 'up_next_music', name: 'Queue', icon: 'speaker', category: 'Home',
  defaultSize: { w: 12, h: 14 }, minSize: { w: 4, h: 4 },
  settings: [
    { key: 'showArt', label: 'Show artwork', type: 'toggle', default: true },
    { key: 'history', label: 'Include previously played', type: 'toggle', default: true },
  ],

  render(host, ctx) {
    const body = el('div.queue');
    host.append(body);
    let data = { past: [], current: null, next: [] };
    let stopSize = null;
    let poll = null;

    const load = async () => {
      try { data = await api.spotifyTimeline(); }
      catch (e) { data = { past: [], current: null, next: [], error: e.message }; }
      draw();
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);

      if (data.error) { body.append(el('div.empty-hint', { text: data.error })); return; }
      if (!data.current && !data.past.length && !data.next.length) {
        // An empty history and an unreadable one are different facts. Saying
        // "nothing played" when Spotify refused the request is how a rate
        // limit spent a day looking like a listening habit.
        body.append(el('div.empty-hint', {
          text: data.history_error
            ? `Recently played unavailable — ${data.history_error}`
            : 'Nothing playing or queued',
        }));
        return;
      }

      const strip = el('div.tl');
      body.append(strip);

      stopSize = autoSize(strip, (w, h, k) => {
        // Wide-and-short reads left to right; anything else reads top to
        // bottom. The row size follows the widget's own scale so the count is
        // computed against what will actually be rendered.
        const horizontal = w > h * 1.6;
        const rowH = 46 * k;
        const colW = 132 * k;
        const capacity = Math.max(1, Math.floor(horizontal ? w / colW : h / rowH));

        const past = ctx.settings.history === false ? [] : data.past;
        const { before, after } = timelineWindow(
          past.length, data.next.length, data.current ? capacity : capacity + 1);

        // A tap means something different in each third of the strip, so the
        // action is decided here, where the position is known, rather than
        // guessed later from the track alone.
        const items = [
          ...past.slice(past.length - before)
            .map(t => ({ t, when: 'past', act: { kind: 'play', track: t } })),
          ...(data.current ? [{ t: data.current, when: 'now', act: null }] : []),
          ...data.next.slice(0, after).map((t, i) => ({
            // Already in the queue: step forward to it. Starting it by URI
            // would replace the queue with that one track, which is exactly
            // the complaint this fixes.
            t, when: 'next', act: { kind: 'skip', steps: i + 1 },
          })),
        ];

        const list = el(`div.tl-list${horizontal ? '.horizontal' : ''}`);
        items.forEach(({ t, when, act }) => {
          const row = el(`div.tl-item.${when}${act ? '.tappable' : ''}`, {}, [
            ctx.settings.showArt !== false && t.art
              ? el('img.tl-art', { src: t.art, alt: '' }) : null,
            el('div.tl-main', {}, [
              el('div.tl-title', { text: t.name }),
              el('div.tl-artist', { text: (t.artists || []).join(', ') }),
            ]),
          ]);
          if (act) bindTap(row, act, data.context_uri);
          list.append(row);
        });
        return list;
      });
    };

    load();
    poll = setInterval(load, 30000);
    const off = bus.on('spotify_changed', load);
    return { refresh: load,
             destroy: () => { off(); clearInterval(poll); if (stopSize) stopSize(); } };
  },
};

/* ---------------------------------------------------------------- your top */

export const TopMusicWidget = {
  type: 'top_music', name: 'Your top music', icon: 'trend', category: 'Home',
  defaultSize: { w: 12, h: 14 }, minSize: { w: 5, h: 5 },
  settings: [
    { key: 'kind', label: 'Show', type: 'select', default: 'artists',
      options: [
        { value: 'artists', label: 'Top artists' },
        { value: 'tracks', label: 'Top tracks' },
      ] },
    { key: 'range', label: 'Over', type: 'select', default: 'medium_term',
      options: [
        { value: 'short_term', label: 'Last 4 weeks' },
        { value: 'medium_term', label: 'Last 6 months' },
        { value: 'long_term', label: 'All time' },
      ] },
    { key: 'count', label: 'How many', type: 'slider', min: 3, max: 30, default: 10 },
  ],

  render(host, ctx) {
    const body = el('div.queue');
    host.append(body);
    let stopSize = null;

    const load = async () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      let data;
      try {
        data = await api.spotifyTop(ctx.settings.kind || 'artists',
                                    ctx.settings.range || 'medium_term');
      } catch (e) {
        // The likeliest cause by far is a token issued before user-top-read was
        // requested, so say what to do rather than just what failed.
        body.append(el('div.empty-hint', {}, [
          el('div', { text: e.message }),
          el('div.field-help', {
            text: 'If this mentions scope, re-authorise Spotify under Settings › Alarms.',
          }),
        ]));
        return;
      }
      body.append(el('div.queue-head', {
        text: `${data.kind === 'tracks' ? 'Top tracks' : 'Top artists'} · ${data.label}`,
      }));
      const holder = el('div.top-holder');
      body.append(holder);
      if (stopSize) { stopSize(); stopSize = null; }
      stopSize = autoSize(holder, (w, h, k) => {
        // A short wide box gets a row of cards; a tall one gets a ranked list.
        // Same data, laid out for the shape it was actually given.
        const cards = w > h * 1.7;
        const list = el(`div.top-list${cards ? '.cards' : ''}`);
        const room = cards ? Math.max(1, Math.floor(w / (96 * k)))
                           : Math.max(1, Math.floor(h / (44 * k)));
        data.items.slice(0, Math.min(Number(ctx.settings.count || 10), room))
          .forEach((it, i) => {
            list.append(el('div.top-item', {}, [
              it.art ? el('img.top-art', { src: it.art, alt: '' }) : null,
              el('div.top-main', {}, [
                el('div.top-name', { text: `${cards ? '' : `${i + 1}. `}${it.name}` }),
                el('div.top-sub', {
                  text: data.kind === 'tracks'
                    ? (it.artists || []).join(', ')
                    : (it.genres || []).join(', '),
                }),
              ]),
            ]));
          });
        return list;
      });
    };

    load();
    const off = bus.on('spotify_changed', load);
    return { refresh: load, destroy: () => { off(); if (stopSize) stopSize(); } };
  },
};

/* ------------------------------------------------------------------ lyrics */

export const LyricsWidget = {
  type: 'lyrics', name: 'Lyrics', icon: 'text', category: 'Home',
  defaultSize: { w: 14, h: 16 }, minSize: { w: 5, h: 5 },
  settings: [
    { key: 'context', label: 'Lines around the current one', type: 'slider',
      min: 1, max: 8, default: 3 },
    { key: 'center', label: 'Keep the current line centred', type: 'toggle', default: true },
  ],

  render(host, ctx) {
    const body = el('div.lyr');
    host.append(body);

    let data = null;
    let fetchedAt = 0;
    let trackKey = '';
    let poll = null;
    let tick = null;
    let lastIdx = -2;
    let lineEls = [];

    const load = async () => {
      try {
        const r = await api.lyricsNow();
        const key = r.track ? `${r.track.name}|${r.track.artists.join(',')}` : '';
        // Only rebuild the DOM when the SONG changes. Rebuilding every poll
        // would fight the scroll and restart the animation four times a minute.
        if (key !== trackKey) { trackKey = key; data = r; lastIdx = -2; draw(); }
        else { data = { ...r, synced: data.synced, plain: data.plain, found: data.found }; }
        fetchedAt = performance.now();
      } catch (e) {
        data = { error: e.message };
        trackKey = '';
        draw();
      }
    };

    const position = () => {
      if (!data || !data.track) return 0;
      const base = data.progress_ms || 0;
      if (!data.playing) return base;
      return Math.min(data.track.duration_ms || 0, base + (performance.now() - fetchedAt));
    };

    /** Same binary search as the server's, for the same reason. */
    const activeLine = (lines, ms) => {
      let lo = 0, hi = lines.length - 1, found = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (lines[mid].ms <= ms) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      return found;
    };

    const draw = () => {
      clear(body);
      lineEls = [];
      if (!data || data.error) {
        body.append(el('div.empty-hint', { text: (data && data.error) || 'No lyrics' }));
        return;
      }
      if (!data.track) {
        body.append(el('div.empty-hint', { text: 'Nothing playing' }));
        return;
      }
      if (data.instrumental) {
        body.append(el('div.empty-hint', { text: 'Instrumental' }));
        return;
      }
      if (!data.found) {
        body.append(el('div.empty-hint', {}, [
          el('div', { text: 'No lyrics found' }),
          el('div.field-help', { text: `Nobody has submitted ${data.track.name} to LRCLIB yet.` }),
        ]));
        return;
      }
      if (!data.synced.length) {
        // Words but no timings — still worth showing, just not followable.
        body.append(el('div.lyr-plain', { text: data.plain }));
        return;
      }
      const list = el('div.lyr-list');
      data.synced.forEach((l) => {
        const node = el('div.lyr-line', { text: l.line || '♪' });
        lineEls.push(node);
        list.append(node);
      });
      body.append(list);
    };

    const paint = () => {
      if (!data || !data.found || !lineEls.length) return;
      const idx = activeLine(data.synced, position());
      if (idx === lastIdx) return;
      lastIdx = idx;
      lineEls.forEach((n, i) => {
        n.classList.toggle('now', i === idx);
        n.classList.toggle('past', i < idx);
      });
      if (ctx.settings.center !== false && idx >= 0 && lineEls[idx]) {
        // NOT scrollIntoView: it scrolls every scrollable ancestor, including
        // the page track — which slides the whole app to this widget's page
        // even when you are looking at another one. Scrolling the list itself
        // is the only thing that should move.
        const list = lineEls[idx].parentElement;
        if (list) {
          const line = lineEls[idx];
          list.scrollTo({
            top: line.offsetTop - (list.clientHeight / 2) + (line.offsetHeight / 2),
            behavior: 'smooth',
          });
        }
      }
    };

    load();
    poll = setInterval(load, 8000);
    tick = setInterval(paint, 250);
    const off = bus.on('spotify_changed', load);
    return {
      refresh: load,
      destroy: () => { off(); clearInterval(poll); clearInterval(tick); },
    };
  },
};
