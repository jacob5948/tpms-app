/* Charts, on top of uPlot (static/vendor/uplot.iife.min.js, MIT).
 *
 * uPlot does the hard parts -- canvas rendering, hit-testing, drag-to-zoom,
 * a live legend that reads out values under the cursor -- and it is one
 * vendored file with no build step, which is what this project needs to keep
 * deploying to a Pi by copying files.
 *
 * What is left here is the app's own shape: a series format of {ts, value}
 * points, the states a fetched chart needs (skeleton, held-stale, error with
 * a retry), a table view of the same numbers, and canvas colours sampled from
 * the page so the charts follow the light/dark theme.
 *
 * There is deliberately no second y axis. Two measures on two scales in one
 * plot invent a correlation out of where the scales happen to line up; two
 * plots sharing an x window (see tpmsRangeBar and opts.sync) say the same
 * thing without the lie.
 */

/* Ordered so that neighbouring slots stay apart for colour-blind readers:
 * worst adjacent pair is ΔE 17.1 under protanopia (validated), where the old
 * green-beside-yellow ordering was 5.5. Assign in slot order, never cycle. */
window.TPMS_COLORS = ['#4d8bf5', '#3fb950', '#a371f7', '#f85149', '#39c5cf', '#f0b849'];

(function () {
  const RANGES = [
    { label: '1h', seconds: 3600 },
    { label: '6h', seconds: 21600 },
    { label: '24h', seconds: 86400 },
    { label: '7d', seconds: 604800 },
    { label: '30d', seconds: 2592000 },
  ];
  const TABLE_ROWS = 500;

  /* Charts that share a sync key show one window between them. uPlot's own
     cursor sync only moves the crosshair; a drag-zoom on one facet has to
     take the others with it, or the pair stops describing one thing. */
  const syncGroups = {};
  let syncing = false;

  function shareWindow(key, from, min, max) {
    if (!key || syncing) return;
    syncing = true;
    (syncGroups[key] || []).forEach(peer => { if (peer !== from) peer.applyWindow(min, max); });
    syncing = false;
  }

  const colourOf = (s, i) => s.color || window.TPMS_COLORS[i % window.TPMS_COLORS.length];
  const esc = t => String(t).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const stamp = t => new Date(t * 1000).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  /* Canvas cannot use currentColor, so sample the page's own ink and fade it.
     Read fresh on every build: the theme can change under a live page. */
  function ink(el, opacity) {
    const parts = getComputedStyle(el).color.match(/[\d.]+/g);
    if (!parts) return '#888';
    return 'rgba(' + parts.slice(0, 3).join(',') + ',' + opacity + ')';
  }

  /* uPlot wants one shared x array. Sensors report on their own schedules, so
     take the union of timestamps and leave a null where a series has nothing;
     spanGaps keeps the line continuous across those. */
  function align(series) {
    const stamps = new Set();
    series.forEach(s => s.points.forEach(p => stamps.add(p.ts)));
    const xs = Array.from(stamps).sort((a, b) => a - b);
    const index = new Map(xs.map((t, i) => [t, i]));
    const columns = series.map(s => {
      const column = new Array(xs.length).fill(null);
      s.points.forEach(p => { column[index.get(p.ts)] = p.value; });
      return column;
    });
    return { xs, columns };
  }

  function decimalsFor(series, given) {
    if (given != null) return given;
    const values = series.flatMap(s => s.points.map(p => p.value));
    const span = Math.max(...values) - Math.min(...values);
    return span >= 10 ? 0 : span >= 1 ? 1 : 2;
  }

  // -- the shared range bar ---------------------------------------------
  //
  // One row of ranges above everything it scopes, rather than a set of
  // buttons inside each chart card: two charts of the same thing must never
  // be able to show two different windows.

  window.tpmsRangeBar = function (el, options) {
    const opts = options || {};
    const charts = opts.charts || [];
    let active = opts.active || 'all';

    function bounds() {
      const all = charts.map(c => c.bounds()).filter(Boolean);
      if (!all.length) return null;
      return [Math.min(...all.map(b => b[0])), Math.max(...all.map(b => b[1]))];
    }

    function draw() {
      const span = bounds();
      // Only offer a range the data could fill, unless the caller can fetch
      // more -- offering "7d" for a 20-minute capture would be a lie.
      const offered = RANGES.filter(
        r => opts.onRange || (span && r.seconds < (span[1] - span[0]) * 1.5)
      );
      if (!offered.length) { el.innerHTML = ''; return; }
      el.classList.add('chart-tools', 'row');
      el.innerHTML = offered.concat([{ label: 'All', seconds: 'all' }]).map(r =>
        '<button type="button" class="chip' + (active === r.seconds ? ' on' : '') +
        '" data-seconds="' + r.seconds + '">' + r.label + '</button>').join('');

      el.querySelectorAll('[data-seconds]').forEach(button => {
        button.addEventListener('click', () => {
          const raw = button.dataset.seconds;
          active = raw === 'all' ? 'all' : Number(raw);
          draw();
          if (opts.onRange) { opts.onRange(active === 'all' ? null : active); return; }
          const span2 = bounds();
          if (!span2) return;
          charts.forEach(c => c.setWindow(
            active === 'all' ? null : [span2[1] - active, span2[1]]));
        });
      });
    }

    draw();
    return {
      refresh: draw,
      active: () => active,
      add(chart) { charts.push(chart); draw(); },
    };
  };

  // -- a chart ------------------------------------------------------------

  window.tpmsChart = function (el, series, options) {
    const opts = options || {};
    const height = opts.height || 220;
    let data = series || [];
    let plot = null;
    let bounds = null;
    let observer = null;

    el.classList.add('chart');
    el.innerHTML =
      '<div class="chart-plot"></div>' +
      '<div class="chart-zoom row" hidden><span class="chart-window muted"></span>' +
      '<button type="button" class="chip">Reset zoom</button></div>' +
      (opts.caption ? '<p class="sub chart-caption"></p>' : '') +
      // noTable is for a chart whose numbers are already tabulated elsewhere
      // on the page; a second copy is noise, not accessibility.
      (opts.noTable ? '' :
        '<details class="chart-table"><summary>Show as table</summary>' +
        '<div class="chart-table-body"></div></details>');
    const host = el.querySelector('.chart-plot');
    const zoomBar = el.querySelector('.chart-zoom');
    const table = el.querySelector('.chart-table');
    const tableBody = el.querySelector('.chart-table-body');
    if (opts.caption) el.querySelector('.chart-caption').textContent = opts.caption;

    // A skeleton the size of the finished plot, so the fetch landing does not
    // shove the page around. Not a spinner in a box.
    host.style.minHeight = (height + 34) + 'px';
    host.innerHTML = '<div class="chart-skeleton" style="height:' + height + 'px"></div>';

    const usable = () => data.filter(s => s.points && s.points.length > 1);

    // -- the table view: the same numbers, reachable without colour -------

    let tableDrawn = false;
    function drawTable() {
      if (!table) return;
      const shown = usable();
      if (!shown.length) {
        tableBody.innerHTML = '<div class="empty">Nothing plotted yet.</div>';
        return;
      }
      const { xs, columns } = align(shown);
      const decimals = decimalsFor(shown, opts.decimals);
      const from = Math.max(0, xs.length - TABLE_ROWS);
      let html = '<div class="scroll"><table><thead><tr><th>Time</th>' +
        shown.map(s => '<th class="num">' + esc(s.name) + '</th>').join('') +
        '</tr></thead><tbody>';
      for (let i = xs.length - 1; i >= from; i--) {
        html += '<tr><td>' + stamp(xs[i]) + '</td>' +
          columns.map(c => '<td class="num">' +
            (c[i] == null ? '—' : c[i].toFixed(decimals)) + '</td>').join('') + '</tr>';
      }
      html += '</tbody></table></div>';
      if (from > 0) {
        html += '<p class="sub">Showing the ' + TABLE_ROWS + ' most recent of ' +
                xs.length + ' plotted points.</p>';
      }
      tableBody.innerHTML = html;
      tableDrawn = true;
    }
    table?.addEventListener('toggle', () => { if (table.open) drawTable(); });

    // -- states -----------------------------------------------------------

    function setLoading(on) {
      // Refetching holds the last render at reduced opacity. Tearing it down
      // for a skeleton would flash and jump on every range click.
      host.classList.toggle('is-loading', !!on);
    }

    function setError(message, retry) {
      if (plot) { plot.destroy(); plot = null; }
      if (observer) { observer.disconnect(); observer = null; }
      host.classList.remove('is-loading');
      zoomBar.hidden = true;
      host.innerHTML = '<div class="chart-error"><p>' + esc(message) + '</p>' +
                       '<button type="button" class="chip">Retry</button></div>';
      host.querySelector('button').addEventListener('click', () => {
        host.innerHTML = '<div class="chart-skeleton" style="height:' + height + 'px"></div>';
        retry && retry();
      });
    }

    // -- drawing ----------------------------------------------------------

    function showZoom() {
      const on = api.zoomed();
      zoomBar.hidden = !on;
      if (on) {
        zoomBar.querySelector('.chart-window').textContent =
          stamp(plot.scales.x.min) + ' – ' + stamp(plot.scales.x.max);
      }
    }
    zoomBar.querySelector('button').addEventListener('click', () => {
      api.reset();
      showZoom();
    });

    function build() {
      if (plot) { plot.destroy(); plot = null; }
      if (observer) { observer.disconnect(); observer = null; }
      host.classList.remove('is-loading');
      zoomBar.hidden = true;
      host.innerHTML = '';

      const shown = usable();
      bounds = shown.length
        ? [Math.min(...shown.flatMap(s => s.points.map(p => p.ts))),
           Math.max(...shown.flatMap(s => s.points.map(p => p.ts)))]
        : null;

      if (table && (tableDrawn || table.open)) drawTable();

      if (!shown.length) {
        // Narrowed to nothing reads differently from never had anything.
        const narrowed = opts.activeRange && opts.activeRange !== 'all';
        host.innerHTML = '<div class="empty">' +
          (narrowed ? 'No readings in this window.'
                    : (opts.empty || 'Not enough readings yet to plot.')) + '</div>';
        return;
      }

      const { xs, columns } = align(shown);
      const decimals = decimalsFor(shown, opts.decimals);
      const grid = { stroke: ink(el, 0.12), width: 1 };
      const axisFont = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      const axisStroke = ink(el, 0.55);
      // formatValue lets a caller label a state rather than a quantity --
      // "audible" reads better on both the axis and the legend than "1".
      const format = opts.formatValue
        ? (v => (v == null ? '—' : opts.formatValue(v)))
        : (v => (v == null ? '—' : v.toFixed(decimals)));

      const config = {
        width: host.clientWidth || 900,
        height: height,
        legend: { live: true },
        scales: {
          x: { time: true },
          y: opts.zeroBased ? { range: (u, lo, hi) => [0, hi > 0 ? hi * 1.1 : 1] } : {},
        },
        axes: [
          { stroke: axisStroke, grid: grid, ticks: grid, font: axisFont },
          {
            scale: 'y', stroke: axisStroke, grid: grid, ticks: grid, font: axisFont,
            values: (u, ticks) => ticks.map(format),
            // Word labels ("audible") need more gutter than "38.6", and a
            // state axis wants its two states rather than five gridlines
            // carrying three copies of the same word.
            size: opts.ySplits ? 72 : 50,
            splits: opts.ySplits ? () => opts.ySplits : undefined,
          },
        ],
        series: [{ label: 'time' }].concat(shown.map((s, i) => {
          const colour = colourOf(s, data.indexOf(s) < 0 ? i : data.indexOf(s));
          const line = {
            label: s.name,
            stroke: colour,
            width: 1.7,
            spanGaps: true,
            value: (u, v) => format(v),
            points: { show: false },
          };
          if (s.kind === 'step') {
            // Presence is a state, not a measurement: it holds its value
            // until it changes, and a sloped line between 0 and 1 would
            // imply a vehicle half-arriving.
            line.paths = uPlot.paths.stepped({ align: 1 });
            line.fill = colour + '40';
          } else if (s.kind === 'bar') {
            line.paths = uPlot.paths.bars({ size: [0.85, 40] });
            line.fill = colour + 'a6';
            line.stroke = colour + 'a6';
            line.width = 0;
          }
          return line;
        })),
        hooks: {
          // Drag-zoom and uPlot's built-in double-click reset both land here.
          setScale: [u => {
            if (!u.scales.x) return;
            showZoom();
            shareWindow(opts.sync, api, u.scales.x.min, u.scales.x.max);
          }],
        },
        cursor: {
          drag: { x: true, y: false },
          // Stacked facets of one window read as a single figure, so the
          // crosshair belongs on both at once.
          sync: opts.sync ? { key: opts.sync, setSeries: false } : undefined,
          // Series report on their own schedules, so the point under the
          // cursor is often null for some of them. Snap each series to its
          // own nearest reading instead of reading out a gap.
          dataIdx: (u, seriesIdx, closestIdx) => {
            const values = u.data[seriesIdx];
            if (values[closestIdx] != null) return closestIdx;
            for (let step = 1; step < values.length; step++) {
              if (values[closestIdx - step] != null) return closestIdx - step;
              if (values[closestIdx + step] != null) return closestIdx + step;
            }
            return closestIdx;
          },
        },
      };

      plot = new uPlot(config, [xs].concat(columns), host);
      host.style.minHeight = '';
      showZoom();

      // uPlot sizes a canvas in pixels, so it needs telling when the panel
      // changes width -- a phone rotating, or the window being resized.
      if (window.ResizeObserver) {
        observer = new ResizeObserver(() => {
          if (plot && host.clientWidth) {
            plot.setSize({ width: host.clientWidth, height: height });
          }
        });
        observer.observe(host);
      }
    }

    // Rebuild on a theme change: the axis colours are baked into the canvas.
    window.matchMedia?.('(prefers-color-scheme: dark)')
      .addEventListener?.('change', () => { if (plot) build(); });

    const api = {
      setSeries(next, activeRange) {
        data = next || [];
        if (activeRange !== undefined) opts.activeRange = activeRange;
        build();
      },
      setCaption(text) {
        let node = el.querySelector('.chart-caption');
        if (!node) {
          node = document.createElement('p');
          node.className = 'sub chart-caption';
          el.querySelector('.chart-plot').after(node);
        }
        node.textContent = text;
      },
      setLoading,
      setError,
      bounds: () => bounds,
      applyWindow(min, max) {
        if (plot && min != null && max != null) plot.setScale('x', { min: min, max: max });
      },
      setWindow(window_) {
        if (!plot || !bounds) return;
        plot.setScale('x', window_
          ? { min: window_[0], max: window_[1] }
          : { min: bounds[0], max: bounds[1] });
      },
      zoomed() {
        if (!plot || !bounds) return false;
        // uPlot has not resolved the scale during the first paint, and a null
        // max compares as "narrower than everything".
        const { min, max } = plot.scales.x;
        if (min == null || max == null) return false;
        return min > bounds[0] + 0.5 || max < bounds[1] - 0.5;
      },
      reset() { if (plot && bounds) plot.setScale('x', { min: bounds[0], max: bounds[1] }); },
    };

    if (opts.sync) (syncGroups[opts.sync] = syncGroups[opts.sync] || []).push(api);
    if (data.length) build();
    return api;
  };
})();
