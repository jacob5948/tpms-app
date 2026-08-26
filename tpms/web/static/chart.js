/* Minimal inline SVG line chart.
 *
 * Shared by the vehicle and sensor pages so both plot the same way. No
 * library: this deploys to a Pi by copying files, and a charting dependency
 * would be the only build step in the project.
 */
window.TPMS_COLORS = ['#4d8bf5', '#3fb950', '#f0b849', '#f85149', '#a371f7', '#39c5cf'];

window.tpmsChart = function (el, series, options) {
  const opts = options || {};
  const live = series.filter(s => s.points && s.points.length > 1);
  if (!live.length) {
    el.innerHTML = '<div class="empty">' +
      (opts.empty || 'Not enough readings yet to plot.') + '</div>';
    return;
  }

  const W = 900, H = opts.height || 220, PAD_X = 44, PAD_Y = 20;
  const all = live.flatMap(s => s.points);
  const xs = all.map(p => p.ts), ys = all.map(p => p.value);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.1 || 1;          // never a zero-height band
  y0 -= pad; y1 += pad;

  // Pick decimals from the range, or a 2 psi spread prints as "34, 34, 35".
  const span = y1 - y0;
  const decimals = opts.decimals != null ? opts.decimals
                 : span >= 10 ? 0 : span >= 1 ? 1 : 2;

  const sx = t => PAD_X + (x1 === x0 ? 0.5 : (t - x0) / (x1 - x0)) * (W - PAD_X * 2);
  const sy = v => H - PAD_Y - (y1 === y0 ? 0.5 : (v - y0) / (y1 - y0)) * (H - PAD_Y * 2);

  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="' +
            (opts.label || 'chart') + '" style="overflow:visible">';

  [0, 0.25, 0.5, 0.75, 1].forEach(f => {
    const v = y0 + (y1 - y0) * f, y = sy(v);
    svg += '<line x1="' + PAD_X + '" x2="' + (W - PAD_X) + '" y1="' + y + '" y2="' + y +
           '" stroke="currentColor" stroke-opacity="0.12"/>' +
           '<text x="6" y="' + (y + 4) + '" font-size="11" fill="currentColor" ' +
           'fill-opacity="0.55">' + v.toFixed(decimals) + '</text>';
  });

  // Time axis: just the ends, which is all a passive capture window needs.
  const fmt = t => new Date(t * 1000).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  svg += '<text x="' + PAD_X + '" y="' + (H - 2) + '" font-size="11" fill="currentColor" ' +
         'fill-opacity="0.55">' + fmt(x0) + '</text>' +
         '<text x="' + (W - PAD_X) + '" y="' + (H - 2) + '" font-size="11" text-anchor="end" ' +
         'fill="currentColor" fill-opacity="0.55">' + fmt(x1) + '</text>';

  live.forEach((s, i) => {
    const colour = s.color || window.TPMS_COLORS[i % window.TPMS_COLORS.length];
    const rows = s.points.slice().sort((a, b) => a.ts - b.ts);
    const d = rows.map((p, j) => (j ? 'L' : 'M') + sx(p.ts).toFixed(1) + ' ' +
                                 sy(p.value).toFixed(1)).join(' ');
    svg += '<path d="' + d + '" fill="none" stroke="' + colour +
           '" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/>';
    // Mark the newest reading, the one people actually look for.
    const last = rows[rows.length - 1];
    svg += '<circle cx="' + sx(last.ts).toFixed(1) + '" cy="' + sy(last.value).toFixed(1) +
           '" r="3" fill="' + colour + '"/>';
  });
  svg += '</svg>';

  const legend = live.length > 1
    ? '<div class="row legend">' + live.map((s, i) =>
        '<span class="key"><i style="background:' +
        (s.color || window.TPMS_COLORS[i % window.TPMS_COLORS.length]) + '"></i>' +
        s.name + '</span>').join('') + '</div>'
    : '';
  el.innerHTML = svg + legend +
    (opts.caption ? '<p class="sub chart-caption">' + opts.caption + '</p>' : '');
};
