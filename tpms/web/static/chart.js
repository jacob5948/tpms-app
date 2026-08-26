/* Charts, on top of uPlot (static/vendor/uplot.iife.min.js, MIT).
 *
 * uPlot does the hard parts -- canvas rendering, hit-testing, drag-to-zoom,
 * a live legend that reads out values under the cursor -- and it is one
 * vendored file with no build step, which is what this project needs to keep
 * deploying to a Pi by copying files.
 *
 * What is left here is the app's own shape: a series format of {ts, value}
 * points, a row of range buttons that can ask the server for a wider window,
 * a second y axis, and colours read from the page so the charts follow the
 * light/dark theme.
 */
window.TPMS_COLORS = ['#4d8bf5', '#3fb950', '#f0b849', '#f85149', '#a371f7', '#39c5cf'];

(function () {
  const RANGES = [
    { label: '1h', seconds: 3600 },
    { label: '6h', seconds: 21600 },
    { label: '24h', seconds: 86400 },
    { label: '7d', seconds: 604800 },
    { label: '30d', seconds: 2592000 },
  ];

  const colourOf = (s, i) => s.color || window.TPMS_COLORS[i % window.TPMS_COLORS.length];

  /* Canvas cannot use currentColor, so sample the page's own ink and fade it.
     Read fresh on every build: the theme can change under a live page. */
  function ink(el, opacity) {
    const colour = getComputedStyle(el).color;
    const parts = colour.match(/[\d.]+/g);
    if (!parts) return colour;
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

  const stamp = t => new Date(t * 1000).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  window.tpmsChart = function (el, series, options) {
    const opts = options || {};
    let data = series || [];
    let plot = null;
    let bounds = null;             // full extent of the loaded data

    el.classList.add('chart');
    el.innerHTML = '<div class="chart-tools row"></div><div class="chart-plot"></div>' +
      (opts.caption ? '<p class="sub chart-caption"></p>' : '');
    const tools = el.querySelector('.chart-tools');
    const host = el.querySelector('.chart-plot');
    if (opts.caption) el.querySelector('.chart-caption').textContent = opts.caption;

    const usable = () => data.filter(s => s.points && s.points.length > 1);

    function zoomed() {
      if (!plot || !bounds) return false;
      return plot.scales.x.min > bounds[0] + 0.5 || plot.scales.x.max < bounds[1] - 0.5;
    }

    // --- range buttons -------------------------------------------------

    function drawTools() {
      if (opts.noTools) { tools.innerHTML = ''; return; }
      // Only offer a range the data could fill, unless the caller can fetch
      // more -- offering "7d" for a 20-minute capture would be a lie. The
      // buttons stay up when a window comes back empty, or there is no way
      // back out of it.
      const offered = RANGES.filter(
        r => opts.onRange || (bounds && r.seconds < (bounds[1] - bounds[0]) * 1.5)
      );
      if (!offered.length && !zoomed()) { tools.innerHTML = ''; return; }

      tools.innerHTML =
        offered.map(r => '<button type="button" class="chip' +
          (opts.activeRange === r.seconds ? ' on' : '') + '" data-seconds="' +
          r.seconds + '">' + r.label + '</button>').join('') +
        '<button type="button" class="chip' + (opts.activeRange === 'all' ? ' on' : '') +
        '" data-seconds="all">All</button>' +
        (zoomed()
          ? '<span class="chart-window muted">' + stamp(plot.scales.x.min) + ' – ' +
            stamp(plot.scales.x.max) + '</span>' +
            '<button type="button" class="chip reset">Reset zoom</button>'
          : '');

      tools.querySelectorAll('[data-seconds]').forEach(button => {
        button.addEventListener('click', () => {
          const seconds = button.dataset.seconds;
          if (opts.onRange) {
            opts.activeRange = seconds === 'all' ? 'all' : Number(seconds);
            opts.onRange(seconds === 'all' ? null : Number(seconds));
            drawTools();
            return;
          }
          if (!plot || !bounds) return;
          opts.activeRange = seconds === 'all' ? 'all' : Number(seconds);
          plot.setScale('x', seconds === 'all'
            ? { min: bounds[0], max: bounds[1] }
            : { min: bounds[1] - Number(seconds), max: bounds[1] });
        });
      });
      tools.querySelector('.reset')?.addEventListener('click', () => {
        plot.setScale('x', { min: bounds[0], max: bounds[1] });
      });
    }

    // --- the plot itself -----------------------------------------------

    let observer = null;

    function build() {
      if (plot) { plot.destroy(); plot = null; }
      if (observer) { observer.disconnect(); observer = null; }
      host.innerHTML = '';

      const shown = usable();
      bounds = shown.length
        ? [Math.min(...shown.flatMap(s => s.points.map(p => p.ts))),
           Math.max(...shown.flatMap(s => s.points.map(p => p.ts)))]
        : null;

      if (!shown.length) {
        // Narrowed to nothing reads differently from never had anything.
        const narrowed = opts.activeRange && opts.activeRange !== 'all';
        host.innerHTML = '<div class="empty">' +
          (narrowed ? 'No readings in this window.'
                    : (opts.empty || 'Not enough readings yet to plot.')) + '</div>';
        drawTools();
        return;
      }

      const { xs, columns } = align(shown);
      const decimals = decimalsFor(shown, opts.decimals);
      const hasRight = shown.some(s => s.axis === 'right');
      const grid = { stroke: ink(el, 0.12), width: 1 };
      const axisFont = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      const axisStroke = ink(el, 0.55);
      const format = v => (v == null ? '—' : v.toFixed(decimals));

      const config = {
        width: host.clientWidth || 900,
        height: opts.height || 220,
        title: undefined,
        legend: { live: true },
        scales: {
          x: { time: true },
          y: opts.zeroBased ? { range: (u, lo, hi) => [0, hi > 0 ? hi * 1.1 : 1] } : {},
          y2: opts.zeroBased ? { range: (u, lo, hi) => [0, hi > 0 ? hi * 1.1 : 1] } : {},
        },
        axes: [
          { stroke: axisStroke, grid: grid, ticks: grid, font: axisFont },
          { scale: 'y', stroke: axisStroke, grid: grid, ticks: grid, font: axisFont,
            values: (u, ticks) => ticks.map(format) },
        ],
        series: [{ label: 'time' }].concat(shown.map((s, i) => {
          const colour = colourOf(s, data.indexOf(s) < 0 ? i : data.indexOf(s));
          const line = {
            label: s.name + (s.axis === 'right' ? ' (right)' : ''),
            scale: s.axis === 'right' ? 'y2' : 'y',
            stroke: colour,
            width: 1.7,
            spanGaps: true,
            value: (u, v) => format(v),
            points: { show: false },
          };
          if (s.kind === 'bar') {
            line.paths = uPlot.paths.bars({ size: [0.85, 40] });
            line.fill = colour + 'a6';
            line.stroke = colour + 'a6';
            line.width = 0;
          }
          return line;
        })),
        cursor: {
          drag: { x: true, y: false },
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
        hooks: {
          // Drag-zoom and the built-in double-click reset both land here, so
          // the window readout and Reset button stay in step with the view.
          setScale: [u => { if (u.scales.x) drawTools(); }],
        },
      };
      if (hasRight) {
        config.axes.push({
          scale: 'y2', side: 1, stroke: axisStroke, font: axisFont,
          grid: { show: false }, ticks: { show: false },
          values: (u, ticks) => ticks.map(format),
        });
      }

      plot = new uPlot(config, [xs].concat(columns), host);
      drawTools();

      // uPlot sizes a canvas in pixels, so it needs telling when the panel
      // changes width -- a phone rotating, or the window being resized.
      if (window.ResizeObserver) {
        observer = new ResizeObserver(() => {
          if (plot && host.clientWidth) {
            plot.setSize({ width: host.clientWidth, height: opts.height || 220 });
          }
        });
        observer.observe(host);
      }
    }

    // Rebuild on a theme change: the axis colours are baked into the canvas.
    window.matchMedia?.('(prefers-color-scheme: dark)')
      .addEventListener?.('change', () => build());

    build();

    return {
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
          el.appendChild(node);
        }
        node.textContent = text;
      },
      zoomed,
      reset() { if (plot && bounds) plot.setScale('x', { min: bounds[0], max: bounds[1] }); },
    };
  };
})();
