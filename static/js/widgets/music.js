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
import { icon } from '../core/icons.js';
import { toast } from '../core/sheet.js';
import { clear, el } from '../core/util.js';

const REFETCH_MS = 8000;    // polite to the API
const TICK_MS = 250;        // smooth enough for a progress bar

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
      } catch (e) { toast(e.message, true); }
    };

    let bar = null;
    let elapsed = null;

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
      const track = el('div.np-bar', {}, [bar]);
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
      if (ctx.settings.showCode && data.code_url) {
        footer.append(el('img.np-code', { src: data.code_url, alt: 'Scan to open in Spotify' }));
      }

      if (footer.childNodes.length) body.append(footer);

      // Tint after layout. The image may already be cached, in which case this
      // resolves in the same frame; if it is a fresh cover the bar starts
      // neutral and settles, which is far better than blocking the render on a
      // network image.
      if (art) {
        artColor(art).then((c) => {
          if (!c || !footer.isConnected) return;
          footer.style.background = c.bg;
          footer.style.color = c.ink;
          // The code is a two-tone SVG; invert it on light bars so the pattern
          // stays scannable rather than disappearing.
          footer.classList.toggle('on-light', c.ink === '#000');
        });
      }

      paintProgress();
    };

    const paintProgress = () => {
      if (!bar || !data || !data.track) return;
      const pos = position();
      const pct = data.track.duration_ms
        ? Math.max(0, Math.min(100, (pos / data.track.duration_ms) * 100)) : 0;
      bar.style.width = `${pct}%`;
      if (elapsed) elapsed.textContent = mmss(pos);
      // The local clock will drift past the end; refetch rather than sit there.
      if (data.playing && data.track.duration_ms
          && pos >= data.track.duration_ms - 400) load();
    };

    load();
    poll = setInterval(load, REFETCH_MS);
    tick = setInterval(paintProgress, TICK_MS);
    const off = bus.on('spotify_changed', load);

    return {
      refresh: load,
      destroy: () => { off(); clearInterval(poll); clearInterval(tick); },
    };
  },
};

/* ------------------------------------------------------------------ up next */

export const UpNextWidget = {
  type: 'up_next_music', name: 'Up next (music)', icon: 'speaker', category: 'Home',
  defaultSize: { w: 12, h: 12 }, minSize: { w: 5, h: 4 },
  settings: [
    { key: 'count', label: 'How many', type: 'slider', min: 3, max: 20, default: 8 },
    { key: 'showArt', label: 'Show artwork', type: 'toggle', default: true },
  ],

  render(host, ctx) {
    const body = el('div.queue');
    host.append(body);
    let poll = null;

    const load = async () => {
      let items = [];
      let error = '';
      try { items = await api.spotifyQueue(); }
      catch (e) { error = e.message; }
      clear(body);
      if (error) { body.append(el('div.empty-hint', { text: error })); return; }
      if (!items.length) {
        body.append(el('div.empty-hint', { text: 'Nothing queued' }));
        return;
      }
      const list = el('div.queue-list');
      items.slice(0, Number(ctx.settings.count || 8)).forEach((t, i) => {
        list.append(el('div.queue-row', {}, [
          el('span.queue-n', { text: String(i + 1) }),
          ctx.settings.showArt !== false && t.art
            ? el('img.queue-art', { src: t.art, alt: '' }) : null,
          el('div.queue-main', {}, [
            el('div.queue-title', { text: t.name }),
            el('div.queue-artist', { text: t.artists.join(', ') }),
          ]),
          el('span.queue-len', { text: mmss(t.duration_ms) }),
        ]));
      });
      body.append(list);
    };

    load();
    poll = setInterval(load, 15000);
    const off = bus.on('spotify_changed', load);
    return { refresh: load, destroy: () => { off(); clearInterval(poll); } };
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

    const load = async () => {
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
      const list = el('div.queue-list');
      data.items.slice(0, Number(ctx.settings.count || 10)).forEach((it, i) => {
        list.append(el('div.queue-row', {}, [
          el('span.queue-n', { text: String(i + 1) }),
          it.art ? el('img.queue-art', { src: it.art, alt: '' }) : null,
          el('div.queue-main', {}, [
            el('div.queue-title', { text: it.name }),
            el('div.queue-artist', {
              text: data.kind === 'tracks'
                ? (it.artists || []).join(', ')
                : (it.genres || []).join(', '),
            }),
          ]),
        ]));
      });
      body.append(list);
    };

    load();
    const off = bus.on('spotify_changed', load);
    return { refresh: load, destroy: off };
  },
};
