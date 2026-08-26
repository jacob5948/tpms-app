/* Minimal inline SVG charts.
 *
 * Shared by the vehicle, sensor and live pages so everything plots the same
 * way. No library: this deploys to a Pi by copying files, and a charting
 * dependency would be the only build step in the project.
 *
 * Beyond drawing, it does the three things a static PNG could not: hover to
 * read values off the line, drag to zoom into a window, and a row of range
 * buttons. Ranges either re-slice the loaded points or, when the caller
 * passes onRange, ask the server for a wider window.
 */
window.TPMS_COLORS = ['#4d8bf5', '#3fb950', '#f0b849', '#f85149', '#a371f7', '#39c5cf'];

(function () {
  const W = 900, PAD_X = 46, PAD_Y = 18, PAD_BOTTOM = 30;
  const RANGES = [
    { label: '1h', seconds: 3600 },
    { label: '6h', seconds: 21600 },
    { label: '24h', seconds: 86400 },
    { label: '7d', seconds: 604800 },
    { label: '30d', seconds: 2592000 },
  ];

  const esc = t => String(t).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* Axis labels get coarser as the window widens: seconds are noise across a
     week, and a date is noise across ten minutes. */
  function timeFormat(span) {
    if (span < 7200) return t => new Date(t * 1000).toLocaleTimeString(undefined,
      { hour: '2-digit', minute: '2-digit' });
    if (span < 172800) return t => new Date(t * 1000).toLocaleString(undefined,
      { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    return t => new Date(t * 1000).toLocaleDateString(undefined,
      { month: 'short', day: 'numeric' });
  }

  const stamp = t => new Date(t * 1000).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  function decimalsFor(span, given) {
    if (given != null) return given;
    return span >= 10 ? 0 : span >= 1 ? 1 : 2;
  }

  function colourOf(series, index) {
    return series.color || window.TPMS_COLORS[index % window.TPMS_COLORS.length];
  }

  window.tpmsChart = function (el, series, options) {
    const opts = options || {};
    const height = opts.height || 220;
    const H = height;
    let data = series || [];
    let view = null;                 // null means the full extent
    let drag = null;

    el.classList.add('chart');
    el.innerHTML =
      '<div class="chart-tools row"></div>' +
      '<div class="chart-plot"><div class="chart-tip" hidden></div></div>' +
      '<div class="chart-legend row legend"></div>' +
      (opts.caption ? '<p class="sub chart-caption">' + esc(opts.caption) + '</p>' : '');
    const tools = el.querySelector('.chart-tools');
    const plot = el.querySelector('.chart-plot');
    const tip = el.querySelector('.chart-tip');
    const legendBox = el.querySelector('.chart-legend');

    const withPoints = () => data.filter(s => s.points && s.points.length);

    function extent() {
      const ts = withPoints().flatMap(s => s.points.map(p => p.ts));
      return ts.length ? [Math.min(...ts), Math.max(...ts)] : null;
    }

    function inView(series) {
      if (!view) return series;
      return series.map(s => Object.assign({}, s, {
        points: (s.points || []).filter(p => p.ts >= view[0] && p.ts <= view[1]),
      }));
    }

    // --- drawing -------------------------------------------------------

    let frame = null;   // geometry of the last render, for hit-testing

    function render() {
      const shown = inView(withPoints()).filter(s => s.points.length);
      drawTools();
      if (!shown.length || shown.every(s => s.points.length < 2)) {
        // Narrowed to nothing is a different message from never had anything,
        // and the range buttons stay up so the window can be widened again.
        const narrowed = view || (opts.activeRange && opts.activeRange !== 'all');
        plot.innerHTML = '<div class="empty">' +
          (narrowed ? 'No readings in this window.'
                    : (opts.empty || 'Not enough readings yet to plot.')) + '</div>';
        plot.appendChild(tip);
        tip.hidden = true;
        legendBox.innerHTML = '';
        frame = null;
        return;
      }

      const xs = shown.flatMap(s => s.points.map(p => p.ts));
      const x0 = Math.min(...xs), x1 = Math.max(...xs);
      const sides = {};
      ['left', 'right'].forEach(side => {
        const vals = shown.filter(s => (s.axis || 'left') === side)
                          .flatMap(s => s.points.map(p => p.value));
        if (!vals.length) return;
        let lo = Math.min(...vals), hi = Math.max(...vals);
        // Counts read wrong if the baseline floats: a bar chart starting at 40
        // makes 41 look like nothing.
        if (opts.zeroBased) lo = Math.min(0, lo);
        const pad = (hi - lo) * 0.1 || Math.max(Math.abs(hi) * 0.1, 1);
        if (!opts.zeroBased) lo -= pad;
        hi += pad;
        sides[side] = { lo, hi, decimals: decimalsFor(hi - lo, opts.decimals) };
      });

      const sx = t => PAD_X + (x1 === x0 ? 0.5 : (t - x0) / (x1 - x0)) * (W - PAD_X * 2);
      const sy = (v, side) => {
        const a = sides[side || 'left'] || sides.left;
        return H - PAD_BOTTOM - (a.hi === a.lo ? 0.5 : (v - a.lo) / (a.hi - a.lo)) *
               (H - PAD_BOTTOM - PAD_Y);
      };

      let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" ' +
                'aria-label="' + esc(opts.label || 'chart') + '" style="overflow:visible">';

      [0, 0.25, 0.5, 0.75, 1].forEach(f => {
        const left = sides.left;
        const y = sy(left.lo + (left.hi - left.lo) * f, 'left');
        svg += '<line x1="' + PAD_X + '" x2="' + (W - PAD_X) + '" y1="' + y + '" y2="' + y +
               '" stroke="currentColor" stroke-opacity="0.12"/>' +
               '<text x="6" y="' + (y + 4) + '" font-size="11" fill="currentColor" ' +
               'fill-opacity="0.55">' +
               (left.lo + (left.hi - left.lo) * f).toFixed(left.decimals) + '</text>';
        if (sides.right) {
          const r = sides.right;
          svg += '<text x="' + (W - PAD_X + 6) + '" y="' + (y + 4) + '" font-size="11" ' +
                 'fill="currentColor" fill-opacity="0.55">' +
                 (r.lo + (r.hi - r.lo) * f).toFixed(r.decimals) + '</text>';
        }
      });

      const fmt = timeFormat(x1 - x0);
      [[x0, PAD_X, 'start'], [(x0 + x1) / 2, W / 2, 'middle'], [x1, W - PAD_X, 'end']]
        .forEach(([t, x, anchor]) => {
          svg += '<text x="' + x + '" y="' + (H - 8) + '" font-size="11" text-anchor="' +
                 anchor + '" fill="currentColor" fill-opacity="0.55">' + fmt(t) + '</text>';
        });

      shown.forEach((s, i) => {
        const colour = colourOf(s, data.indexOf(s) < 0 ? i : data.indexOf(s));
        const rows = s.points.slice().sort((a, b) => a.ts - b.ts);
        const side = s.axis || 'left';
        if (s.kind === 'bar') {
          const width = Math.max(1.2, (W - PAD_X * 2) / Math.max(rows.length, 1) - 1);
          const base = sy(Math.max(sides[side].lo, 0), side);
          rows.forEach(p => {
            const y = sy(p.value, side);
            svg += '<rect x="' + (sx(p.ts) - width / 2).toFixed(1) + '" y="' + Math.min(y, base).toFixed(1) +
                   '" width="' + width.toFixed(1) + '" height="' + Math.max(Math.abs(base - y), 0.6).toFixed(1) +
                   '" fill="' + colour + '" fill-opacity="0.65"/>';
          });
        } else {
          const d = rows.map((p, j) => (j ? 'L' : 'M') + sx(p.ts).toFixed(1) + ' ' +
                                       sy(p.value, side).toFixed(1)).join(' ');
          svg += '<path d="' + d + '" fill="none" stroke="' + colour +
                 '" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/>';
          const last = rows[rows.length - 1];
          svg += '<circle cx="' + sx(last.ts).toFixed(1) + '" cy="' + sy(last.value, side).toFixed(1) +
                 '" r="3" fill="' + colour + '"/>';
        }
      });

      // Crosshair, hover markers and the drag rectangle, all drawn on demand.
      svg += '<g class="chart-hover" style="display:none">' +
             '<line y1="' + PAD_Y + '" y2="' + (H - PAD_BOTTOM) +
             '" stroke="currentColor" stroke-opacity="0.35" stroke-dasharray="3 3"/></g>' +
             '<rect class="chart-band" y="' + PAD_Y + '" height="' + (H - PAD_BOTTOM - PAD_Y) +
             '" fill="currentColor" fill-opacity="0.10" style="display:none"/>' +
             '<rect class="chart-surface" x="' + PAD_X + '" y="' + PAD_Y + '" width="' +
             (W - PAD_X * 2) + '" height="' + (H - PAD_BOTTOM - PAD_Y) +
             '" fill="transparent" style="cursor:crosshair"/>';
      svg += '</svg>';
      plot.innerHTML = svg;
      plot.appendChild(tip);
      tip.hidden = true;

      legendBox.innerHTML = shown.length > 1
        ? shown.map((s, i) => '<span class="key"><i style="background:' +
            colourOf(s, data.indexOf(s) < 0 ? i : data.indexOf(s)) + '"></i>' +
            esc(s.name) + (s.axis === 'right' ? ' <span class="muted">(right)</span>' : '') +
            '</span>').join('')
        : '';

      frame = { shown, sx, sy, x0, x1, sides };
      wireSurface();
    }

    // --- range buttons -------------------------------------------------

    function drawTools() {
      if (opts.noTools) { tools.innerHTML = ''; return; }
      const span = extent();
      // Only offer a range the data could actually fill, unless the caller can
      // fetch more -- offering "7d" for a 20-minute capture is a lie. When a
      // window comes back empty the buttons must stay regardless, or there is
      // no way back out of it.
      const usable = RANGES.filter(
        r => opts.onRange || (span && r.seconds < (span[1] - span[0]) * 1.5)
      );
      if (!usable.length && !view) { tools.innerHTML = ''; return; }
      tools.innerHTML =
        usable.map(r => '<button type="button" class="chip" data-seconds="' + r.seconds +
                        '">' + r.label + '</button>').join('') +
        '<button type="button" class="chip" data-seconds="all">All</button>' +
        (view ? '<span class="chart-window muted">' + stamp(view[0]) + ' – ' +
                stamp(view[1]) + '</span>' : '') +
        (view ? '<button type="button" class="chip reset">Reset zoom</button>' : '');

      tools.querySelectorAll('[data-seconds]').forEach(button => {
        button.addEventListener('click', () => {
          const seconds = button.dataset.seconds;
          if (opts.onRange) {
            tools.querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
            button.classList.add('on');
            view = null;
            opts.onRange(seconds === 'all' ? null : Number(seconds));
            return;
          }
          const [, hi] = extent();
          view = seconds === 'all' ? null : [hi - Number(seconds), hi];
          render();
        });
      });
      tools.querySelector('.reset')?.addEventListener('click', () => { view = null; render(); });
      if (opts.activeRange) {
        tools.querySelector('[data-seconds="' + opts.activeRange + '"]')?.classList.add('on');
      }
    }

    // --- hover and drag ------------------------------------------------

    function dataX(event) {
      const svg = plot.querySelector('svg');
      const box = svg.getBoundingClientRect();
      const px = PAD_X + ((event.clientX - box.left) / box.width) * W - PAD_X;
      const frac = (px - PAD_X) / (W - PAD_X * 2);
      return { px, ts: frame.x0 + Math.min(Math.max(frac, 0), 1) * (frame.x1 - frame.x0) };
    }

    function nearest(points, ts) {
      let best = null, gap = Infinity;
      for (const p of points) {
        const d = Math.abs(p.ts - ts);
        if (d < gap) { gap = d; best = p; }
      }
      return best;
    }

    function showTip(ts, clientX) {
      const svg = plot.querySelector('svg');
      const hover = svg.querySelector('.chart-hover');
      const x = frame.sx(ts);
      hover.style.display = '';
      const line = hover.querySelector('line');
      line.setAttribute('x1', x); line.setAttribute('x2', x);
      hover.querySelectorAll('circle').forEach(c => c.remove());

      const lines = [];
      frame.shown.forEach((s, i) => {
        const p = nearest(s.points, ts);
        if (!p) return;
        const colour = colourOf(s, data.indexOf(s) < 0 ? i : data.indexOf(s));
        const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        dot.setAttribute('cx', frame.sx(p.ts));
        dot.setAttribute('cy', frame.sy(p.value, s.axis || 'left'));
        dot.setAttribute('r', '3.5');
        dot.setAttribute('fill', colour);
        hover.appendChild(dot);
        const side = frame.sides[s.axis || 'left'];
        lines.push('<span class="key"><i style="background:' + colour + '"></i>' +
                   (frame.shown.length > 1 ? esc(s.name) + ' ' : '') +
                   '<b>' + p.value.toFixed(side.decimals) + '</b></span>');
      });

      const at = nearest(frame.shown[0].points, ts);
      tip.innerHTML = '<div class="when">' + stamp(at ? at.ts : ts) + '</div>' + lines.join('');
      tip.hidden = false;
      const box = plot.getBoundingClientRect();
      const left = Math.min(Math.max(clientX - box.left + 12, 4), box.width - tip.offsetWidth - 4);
      tip.style.left = left + 'px';
    }

    function hideTip() {
      tip.hidden = true;
      const hover = plot.querySelector('.chart-hover');
      if (hover) hover.style.display = 'none';
    }

    function wireSurface() {
      const svg = plot.querySelector('svg');
      const surface = svg.querySelector('.chart-surface');
      const band = svg.querySelector('.chart-band');
      if (!surface) return;

      surface.addEventListener('pointermove', event => {
        const { ts } = dataX(event);
        showTip(ts, event.clientX);
        if (drag !== null) {
          const from = Math.min(frame.sx(drag), frame.sx(ts));
          const to = Math.max(frame.sx(drag), frame.sx(ts));
          band.style.display = '';
          band.setAttribute('x', from);
          band.setAttribute('width', Math.max(to - from, 0));
        }
      });
      surface.addEventListener('pointerleave', () => { hideTip(); });
      surface.addEventListener('pointerdown', event => {
        drag = dataX(event).ts;
        surface.setPointerCapture(event.pointerId);
      });
      surface.addEventListener('pointerup', event => {
        if (drag === null) return;
        const to = dataX(event).ts;
        const lo = Math.min(drag, to), hi = Math.max(drag, to);
        drag = null;
        band.style.display = 'none';
        // A click is a drag of zero width; it should not zoom to nothing.
        if (hi - lo > (frame.x1 - frame.x0) / 200) { view = [lo, hi]; render(); }
      });
      surface.addEventListener('dblclick', () => { view = null; render(); });
    }

    render();

    return {
      /** Replace the plotted data, keeping the tools wired. Used by pages that
       *  refetch a wider window from the server. */
      setSeries(next, activeRange) {
        data = next || [];
        view = null;
        if (activeRange !== undefined) opts.activeRange = activeRange;
        render();
      },
      reset() { view = null; render(); },
      /** True while the user is looking at a window they chose. Pages that
       *  poll use it to leave a zoomed chart alone. */
      zoomed() { return view !== null; },
      setCaption(text) {
        let node = el.querySelector('.chart-caption');
        if (!node) {
          node = document.createElement('p');
          node.className = 'sub chart-caption';
          el.appendChild(node);
        }
        node.textContent = text;
      },
    };
  };
})();
