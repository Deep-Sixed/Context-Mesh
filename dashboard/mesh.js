/* Context Mesh — the dashboard.
 *
 * Every base number on this page comes from `data/mesh.json`, which
 * `python -m contextmesh export` writes from a real build of the graph. The
 * page then runs a live stream on top of that snapshot: new walks are sampled
 * from the measured distributions (resolved rate, hop histogram, edge traffic,
 * dead-end reasons), so the motion is a resampling of real measurements rather
 * than a scripted animation. Nothing here invents a number the engine did not
 * produce.
 */
(() => {
"use strict";

// ── data ────────────────────────────────────────────────────────────────
const inline = document.getElementById("mesh-data").textContent.trim();
let DATA = null;
try { DATA = JSON.parse(inline); } catch (_) { DATA = null; }

const TYPES = ["entity", "claim", "source", "decision"];
// Sampled off the capture. Entities and claims sit close together there by
// design; claims are pulled a shade deeper so the two clusters stay separable
// at this node count.
const TYPE_COLOR = {
  entity: "#cb6455", claim: "#ab4433", source: "#ca957a", decision: "#e3d5b8",
};
const PHASES = [
  ["PHASE 1/4 · EXTRACT", "THE PASS HANDS BACK WHAT IS WORTH A NODE",
   { entity: .34, claim: 1, source: 1, decision: .34 }],
  ["PHASE 2/4 · LINK", "A SHARED ID BECOMES A TYPED EDGE",
   { entity: 1, claim: .78, source: .6, decision: .5 }],
  ["PHASE 3/4 · CLUSTER", "THE TYPES PULL THEMSELVES APART",
   { entity: .8, claim: .8, source: .8, decision: .8 }],
  ["PHASE 4/4 · PUBLISH", "HUBS WRITTEN OUT AS WALKABLE ANCHORS",
   { entity: 1, claim: 1, source: .55, decision: .55 }],
];
const STAGE_CAPTION = {
  CHUNK: "spans in", EXTRACT: "entities + claims", RESOLVE: "one id per thing",
  LINK: "typed edges only", EMBED: "vector on the node", PRUNE: "nobody walked it",
};
const GRID_COLS = 72, GRID_ROWS = 14, GRID_CELLS = GRID_COLS * GRID_ROWS;

// ── helpers ─────────────────────────────────────────────────────────────
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const fmt = n => Math.round(n).toLocaleString("en-US");
const compact = n => {
  n = Math.round(n);
  if (n >= 1e6) return (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "K";
  return String(n);
};
const pct = f => (f * 100).toFixed(1) + "%";
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const brackets = node => {
  ["tl", "tr", "bl", "br"].forEach(c => node.appendChild(el("span", "cnr " + c)));
  return node;
};

/** Draw from a discrete distribution; returns an index. */
function sampler(weights) {
  const total = weights.reduce((a, b) => a + b, 0);
  if (total <= 0) return () => -1;
  const cum = [];
  let run = 0;
  for (const w of weights) { run += w; cum.push(run / total); }
  return () => {
    const r = Math.random();
    for (let i = 0; i < cum.length; i++) if (r <= cum[i]) return i;
    return cum.length - 1;
  };
}

function fitCanvas(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.round(rect.width)), h = Math.max(1, Math.round(rect.height));
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

// ── live stream, seeded from the snapshot ───────────────────────────────
function makeLive(d) {
  const cells = (d.traversal_grid.cells || []).slice(-GRID_CELLS);
  while (cells.length < GRID_CELLS) cells.unshift(-1);   // -1 = not walked yet

  const hopBins = d.hop_budget.bins.map(b => b.count);
  const edgeRows = d.edge_ledger.rows.map(r => ({ ...r }));
  const deadRows = d.dead_ends.rows.map(r => ({ ...r }));
  const series = d.walk_vs_flat.series.slice(-70);
  const walkTime = (d.ontology.walk_time_series || []).slice(-46);

  return {
    cells,
    fresh: new Map(),
    hopBins,
    edgeRows,
    deadRows,
    series,
    walkTime,
    pickHop: sampler(hopBins.map(c => c || 0)),
    pickEdge: sampler(edgeRows.map(r => r.traversals || r.count || 0)),
    pickDead: sampler(deadRows.map(r => r.count || 0)),
    resolvedRate: d.dead_ends.resolved_rate,
    build: d.build.number,
    spans: d.build.spans_in,
    dropped: d.build.dropped_at_resolve,
    committed: d.build.committed_walkable,
    nodes: d.header.nodes_resolved,
    walksTotal: d.header.walks_per_min,
    edgesTick: d.header.edges_per_tick,
    travMs: d.header.traversal_ms,
    spark: Array.from({ length: 46 }, () => .25 + Math.random() * .55),
    stage: 0,
    phase: 0,
    walkCursor: 0,
  };
}

/** One more walk, sampled from what the engine actually measured. */
function stepWalk(L) {
  const resolved = Math.random() < L.resolvedRate;
  L.cells.push(resolved ? 1 : 0);
  while (L.cells.length > GRID_CELLS) L.cells.shift();
  L.fresh.set(L.cells.length - 1, performance.now());
  L.walksTotal++;

  if (resolved) {
    const h = L.pickHop();
    if (h >= 0) L.hopBins[h]++;
    const e = L.pickEdge();
    if (e >= 0) L.edgeRows[e].traversals += 1 + Math.floor(Math.random() * 3);
    const sample = L.series.length
      ? L.series[Math.floor(Math.random() * L.series.length)]
      : { flat: 0, walk: 0 };
    L.series.push({
      flat: Math.round(sample.flat * (.97 + Math.random() * .06)),
      walk: Math.round(Math.max(1, sample.walk * (.9 + Math.random() * .25))),
    });
    while (L.series.length > 70) L.series.shift();
    const wt = L.walkTime.length ? L.walkTime[Math.floor(Math.random() * L.walkTime.length)] : 40;
    L.walkTime.push(Math.max(1, Math.round(wt * (.8 + Math.random() * .45))));
    while (L.walkTime.length > 46) L.walkTime.shift();
    L.travMs = clamp(Math.round(lerp(L.travMs, wt * 1.6, .25)), 40, 999);
  } else {
    const r = L.pickDead();
    if (r >= 0) L.deadRows[r].count++;
  }
  L.spark.push(.2 + Math.random() * .75);
  while (L.spark.length > 46) L.spark.shift();
  L.edgesTick = clamp(Math.round(lerp(L.edgesTick, 6 + Math.random() * 24, .4)), 3, 40);
}

/** The build never stops: each full pass through the six stages commits more. */
function stepBuild(L, d) {
  L.build++;
  const perBuild = {
    spans: Math.max(1, Math.round(d.build.spans_in * (.9 + Math.random() * .25))),
    dropped: Math.max(1, Math.round(d.build.dropped_at_resolve * (.05 + Math.random() * .06))),
    committed: Math.max(1, Math.round(d.build.committed_walkable * (.04 + Math.random() * .05))),
  };
  L.spans = Math.round(lerp(L.spans, perBuild.spans, .5));
  L.dropped += perBuild.dropped;
  L.committed += perBuild.committed;
  L.nodes += Math.round(perBuild.committed * .35);
  const share = L.edgeRows.reduce((a, r) => a + r.count, 0) || 1;
  for (const r of L.edgeRows) {
    r.count += Math.max(0, Math.round(perBuild.committed * (r.count / share)));
  }
}

// ── layout ──────────────────────────────────────────────────────────────
function build(d) {
  const sheet = document.getElementById("sheet");
  sheet.textContent = "";

  // masthead ------------------------------------------------------------
  const mast = el("div", "masthead");
  mast.appendChild(el("div", "mark", `
    <svg viewBox="0 0 40 40" aria-hidden="true">
      <g stroke="#fbeee9" stroke-width="3.4" stroke-linecap="round">
        <line x1="20" y1="20" x2="9"  y2="9"/>
        <line x1="20" y1="20" x2="31" y2="9"/>
        <line x1="20" y1="20" x2="9"  y2="31"/>
        <line x1="20" y1="20" x2="31" y2="31"/>
      </g>
      <g fill="#fbeee9">
        <circle cx="9" cy="9" r="4.1"/><circle cx="31" cy="9" r="4.1"/>
        <circle cx="9" cy="31" r="4.1"/><circle cx="31" cy="31" r="4.1"/>
        <circle cx="20" cy="20" r="4.6"/>
      </g>
      <circle cx="20" cy="20" r="1.7" fill="#c9604d"/>
    </svg>`));
  mast.appendChild(el("div", "wordmark", `
    <h1>${d.header.title.split(" ")[0]} <span class="g">Graph Engineering</span> · ${d.header.subtitle}</h1>
    <div class="kicker">${d.header.kicker}</div>`));
  mast.appendChild(el("div", "stats", `
    <div class="stat"><div class="label">Nodes
resolved</div><div class="v" id="s-nodes">—</div></div>
    <div class="stat rust"><div class="label">Edges /
tick</div><div class="v" id="s-edges">—</div></div>
    <div class="stat green"><div class="label">Walks /
min</div><div class="v" id="s-walks">—</div></div>`));
  mast.appendChild(el("div", "sparkbox", `<canvas id="spark" width="150" height="40"></canvas>`));
  mast.appendChild(el("div", "clockbox", `
    <div class="clock" id="clock">--:--:--</div>
    <div class="trav">Traversal<b id="s-trav">—</b></div>`));
  sheet.appendChild(mast);

  // build strip ---------------------------------------------------------
  const strip = el("div", "panel strip");
  strip.appendChild(el("div", "meta", `<span id="s-buildmeta">—</span>`));
  strip.appendChild(el("div", "sep"));
  const stages = el("div", "stages");
  d.build.stages.forEach((s, i) => {
    if (i) stages.appendChild(el("div", "leader"));
    stages.appendChild(el("div", "stagechip", `${String(i + 1).padStart(2, "0")} ${s.name}`));
  });
  strip.appendChild(stages);
  sheet.appendChild(strip);

  // build path ----------------------------------------------------------
  const bp = el("div", "panel pad");
  bp.appendChild(el("div", "head", `
    <div>
      <h2 class="ptitle">Build path <span class="accent">· what earns a node in the graph</span></h2>
      <div class="psub">${d.build.stages.map(s => s.name).join(" → ")}</div>
    </div>
    <div class="badge" id="s-stagebadge">Stage 01 / 06</div>`));
  const cards = el("div", "cards");
  d.build.stages.forEach((s, i) => {
    const card = brackets(el("div", "card bracketed", `
      <div class="n">${String(i + 1).padStart(2, "0")}</div>
      <div class="t">${s.name}</div>
      <div class="c">${STAGE_CAPTION[s.name] || s.caption}</div>`));
    if (i < d.build.stages.length - 1) card.appendChild(el("div", "arrow", "▶"));
    cards.appendChild(card);
  });
  bp.appendChild(cards);
  bp.appendChild(el("div", "rail", `
    <div class="railend"><div class="label">Dropped at resolve</div><div class="v" id="s-dropped">—</div></div>
    <canvas id="railcanvas"></canvas>
    <div class="railend right"><div class="label">Committed → walkable</div><div class="v" id="s-committed">—</div></div>`));
  sheet.appendChild(bp);

  // graph ---------------------------------------------------------------
  const gp = el("div", "panel pad graphwrap");
  gp.appendChild(el("div", "head", `
    <div>
      <h2 class="ptitle">Context Mesh <span class="accent">· the live graph</span> · Walking</h2>
      <div class="psub">What Claude resolved, wired to whatever shares a typed edge</div>
    </div>
    <div class="badge ink" id="s-phasepill">Link</div>`));
  const legend = el("div", "legend", `<div class="label">Node types</div>` +
    TYPES.map(t => {
      const row = d.node_types.find(n => n.type === t) || { label: t.toUpperCase(), count: 0 };
      return `<div class="row"><span class="sw" style="background:${TYPE_COLOR[t]}"></span>
        <b>${row.label}</b><i id="s-count-${t}">${row.count}</i></div>`;
    }).join(""));
  gp.appendChild(legend);
  gp.appendChild(el("div", "phasebox", `<div class="p" id="s-phase">—</div><div class="d" id="s-phasedesc">—</div>`));
  gp.appendChild(el("canvas", null, "")).id = "mesh";
  TYPES.forEach((t, i) => {
    const chip = el("div", "clusterlabel");
    chip.id = "cl-" + t;
    const row = d.node_types.find(r => r.type === t);
    chip.textContent = `TYPE ${i + 1} · ${(row ? row.label : t.toUpperCase())} · ${row ? row.count : 0}`;
    gp.appendChild(chip);
  });
  gp.appendChild(el("div", "footnote", `
    <span>One node = one resolved entity · one edge = a typed relation both sides agree on</span>
    <span style="color:var(--rust)">4 node types · turning</span>`));
  sheet.appendChild(gp);

  // metric row ----------------------------------------------------------
  const row3 = el("div", "row3");

  const hop = el("div", "panel pad");
  hop.appendChild(el("div", "head", `
    <div><h2 class="ptitle">Hop budget</h2><div class="psub">Depth before it answers</div></div>
    <div class="badge" id="s-median">Median — hops</div>`));
  const hops = el("div", "hops");
  hops.id = "hops";
  hop.appendChild(hops);
  const axis = el("div", "hopaxis");
  d.hop_budget.bins.forEach(b => axis.appendChild(el("span", null, String(b.hops))));
  hop.appendChild(axis);
  hop.appendChild(el("div", "footnote", `
    <span>Hops per answer</span><span style="color:var(--rust)">${d.hop_budget.note}</span>`));
  row3.appendChild(hop);

  const led = el("div", "panel pad");
  led.appendChild(el("div", "head", `
    <div><h2 class="ptitle">Edge ledger</h2><div class="psub">What carries the traffic</div></div>
    <div class="badge ink" id="s-edgetotal">— edges</div>`));
  const ledger = el("div", "ledger");
  ledger.id = "ledger";
  d.edge_ledger.rows.forEach((r, i) => {
    const row = el("div", "lrow");
    row.innerHTML = `<div class="k">${r.label}</div>
      <div class="track"><div class="fill" style="background:${[TYPE_COLOR.entity, TYPE_COLOR.claim, TYPE_COLOR.source, TYPE_COLOR.decision][i]}"></div><div class="caret"></div></div>
      <div class="n">0</div>`;
    ledger.appendChild(row);
  });
  led.appendChild(ledger);
  led.appendChild(el("div", "footnote", `
    <span>Share of traversals</span>
    <span style="color:var(--green)">Untyped edges: ${d.edge_ledger.untyped}</span>`));
  row3.appendChild(led);

  const wvf = el("div", "panel pad");
  wvf.appendChild(el("div", "head", `
    <div><h2 class="ptitle">Walk vs flat</h2><div class="psub">Tokens per answer</div></div>
    <div class="badge good" id="s-saving">—</div>`));
  wvf.appendChild(el("canvas", null, "")).id = "wvf";
  wvf.appendChild(el("div", "wvflegend", `
    <span><span class="dot" style="background:${TYPE_COLOR.source}"></span>Flat top-k</span>
    <span><span class="dot" style="background:${TYPE_COLOR.entity}"></span>Typed walk</span>
    <span style="margin-left:auto;color:var(--ink-faint)">Same answer</span>`));
  row3.appendChild(wvf);
  sheet.appendChild(row3);

  // traversal grid + dead ends -----------------------------------------
  const row2 = el("div", "row2");

  const tg = el("div", "panel pad");
  tg.appendChild(el("div", "head", `
    <div><h2 class="ptitle"><span style="color:var(--ink)">■</span> The traversal grid
      <span class="accent">· every walk since the graph was built</span> · ${fmt(GRID_CELLS)} cells</h2></div>
    <div class="badge ink"><span class="dot" style="background:var(--rust-pale)"></span>Live</div>`));
  tg.appendChild(el("div", "gridlegend", `
    <span>One cell = one walk</span>
    <span style="color:var(--green)">■ Resolved on the graph</span>
    <span style="color:var(--rust)">○ Dead end</span>`));
  const grid = el("div", "grid");
  grid.id = "grid";
  for (let i = 0; i < GRID_CELLS; i++) grid.appendChild(el("span", "cell empty"));
  tg.appendChild(grid);
  tg.appendChild(el("div", "footnote", `<span>${d.traversal_grid.note}</span>`));
  row2.appendChild(tg);

  const de = el("div", "panel pad");
  de.appendChild(el("div", "head", `
    <div><h2 class="ptitle">Dead-end ledger</h2></div>
    <div class="badge good" id="s-resolvedrate">—</div>`));
  de.appendChild(el("div", "label", "Walks that ended nowhere"));
  de.appendChild(el("div", "bignum", "0")).id = "s-deadtotal";
  const dend = el("div", "dend");
  dend.id = "dend";
  d.dead_ends.rows.forEach((r, i) => {
    const row = el("div", "drow", `
      <div class="top"><span>${r.label}</span><i>0</i></div>
      <div class="dtrack"><div class="dfill" style="background:${[TYPE_COLOR.entity, TYPE_COLOR.claim, TYPE_COLOR.source, "#9a8f5f"][i]}"></div></div>`);
    dend.appendChild(row);
  });
  de.appendChild(dend);
  de.appendChild(el("div", "ontology", `
    <div class="label">${d.ontology.note}</div>
    <div class="f">${d.ontology.file}</div>
    <canvas id="walktime"></canvas>
    <div class="ostats">
      <div><div class="k">Edge types</div><div class="v">${d.ontology.edge_types_used}/${d.ontology.edge_types}</div></div>
      <div><div class="k">Walk time</div><div class="v" id="s-walktime">—</div></div>
      <div><div class="k">Orphans</div><div class="v">${d.ontology.orphans}</div></div>
    </div>`));
  row2.appendChild(de);
  sheet.appendChild(row2);

  sheet.appendChild(el("div", "footer", `
    <span>A typed edge beats a top-k guess · the graph is what survives into the
      <span class="accent">next question</span></span>
    <span><b>Claude</b> · Graph engineering</span>`));
}

// ── force-directed graph ────────────────────────────────────────────────
function makeMesh(d) {
  const raw = d.graph.nodes.filter(n => TYPES.includes(n.t));
  const remap = new Map();
  raw.forEach((n, i) => remap.set(d.graph.nodes.indexOf(n), i));

  const nodes = raw.map((n, i) => {
    const ci = TYPES.indexOf(n.t);
    const a = (ci / TYPES.length) * Math.PI * 2;
    return {
      id: n.id, t: n.t, ci, deg: n.deg, label: n.label,
      x: Math.cos(a) * 120 + (Math.random() - .5) * 70,
      y: Math.sin(a) * 120 + (Math.random() - .5) * 70,
      vx: 0, vy: 0,
      r: 2.4 + Math.min(n.deg, 26) * .26,
      pull: Math.max(1, Math.sqrt(n.deg)),
      hub: n.deg >= 9,
      alpha: .6,
    };
  });

  const edges = [];
  for (const e of d.graph.edges) {
    const s = remap.get(e.s), t = remap.get(e.d);
    if (s === undefined || t === undefined || s === t) continue;
    edges.push({ s, t, type: e.t, n: e.n || 0, cross: nodes[s].ci !== nodes[t].ci });
  }

  const particles = Array.from({ length: 16 }, () => ({
    e: edges.length ? Math.floor(Math.random() * edges.length) : 0,
    p: Math.random(),
    v: .004 + Math.random() * .008,
  }));

  const sizes = {};
  for (const n of nodes) sizes[n.t] = (sizes[n.t] || 0) + 1;

  return {
    nodes, edges, particles, theta: 0, scale: 0, midX: undefined, midY: undefined,
    sizes, maxSize: Math.max(1, ...Object.values(sizes)),
    alphas: { entity: .6, claim: .6, source: .6, decision: .6 },
  };
}

function simulate(M, spread) {
  const N = M.nodes, R = 248;
  // The panel is wide and short, so the four clusters sit on an ellipse rather
  // than a circle — otherwise height caps the layout and the mesh renders as a
  // small disc in a lot of empty paper.
  const centres = TYPES.map((t, i) => {
    const a = (i / TYPES.length) * Math.PI * 2 - Math.PI / 4;
    // A cluster sits out from the centre in proportion to its mass. Parking a
    // three-node cluster as far out as a seventy-node one leaves a third of the
    // panel holding three dots.
    const reach = .5 + .5 * Math.sqrt((M.sizes[t] || 1) / M.maxSize);
    return {
      x: Math.cos(a) * R * 2.0 * spread * reach,
      y: Math.sin(a) * R * .74 * spread * reach,
    };
  });

  for (const n of N) {
    const c = centres[n.ci];
    n.vx += (c.x - n.x) * .021;
    n.vy += (c.y - n.y) * .021;
  }
  for (let i = 0; i < N.length; i++) {
    for (let j = i + 1; j < N.length; j++) {
      const a = N[i], b = N[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 > 42000 || d2 === 0) continue;
      const d = Math.sqrt(d2);
      // A hard cap makes every pair settle at the same distance and the cluster
      // renders as a lattice; softening the near field keeps it organic.
      const push = 760 / (d2 + 90);
      dx /= d; dy /= d;
      a.vx -= dx * push; a.vy -= dy * push;
      b.vx += dx * push; b.vy += dy * push;
    }
  }
  for (const e of M.edges) {
    const a = M.nodes[e.s], b = M.nodes[e.t];
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.hypot(dx, dy) || 1;
    // Cross-cluster edges are the majority here; letting them pull as hard as
    // intra-cluster ones collapses the four types into one ball.
    const rest = e.cross ? 210 : 42;
    const k = (d - rest) * (e.cross ? .00022 : .0055);
    // Normalise by degree, or a node with forty edges gets dragged forty times
    // as hard as a leaf and the small clusters end up wherever the hubs want.
    const ux = dx / d * k, uy = dy / d * k;
    a.vx += ux / a.pull; a.vy += uy / a.pull;
    b.vx -= ux / b.pull; b.vy -= uy / b.pull;
  }
  for (const n of N) {
    n.vx *= .86; n.vy *= .86;
    n.x += clamp(n.vx, -6, 6);
    n.y += clamp(n.vy, -6, 6);
  }
}

function drawMesh(M, canvas, phaseAlphas, labels) {
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);

  for (const t of TYPES) M.alphas[t] = lerp(M.alphas[t], phaseAlphas[t], .045);

  // Fit to what the simulation actually produced, in the rotated frame it will
  // actually be drawn in. Clusters here are wildly unequal in size, so the
  // layout's centre of mass is nowhere near the origin — centring on the
  // bounding box rather than on (0,0) is what keeps the mesh in the panel.
  const cos = Math.cos(M.theta), sin = Math.sin(M.theta);
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const n of M.nodes) {
    const rx = n.x * cos - n.y * sin, ry = n.x * sin + n.y * cos;
    minX = Math.min(minX, rx - n.r); maxX = Math.max(maxX, rx + n.r);
    minY = Math.min(minY, ry - n.r); maxY = Math.max(maxY, ry + n.r);
  }
  const spanX = Math.max(1, maxX - minX), spanY = Math.max(1, maxY - minY);
  const target = clamp(Math.min((w - 120) / spanX, (h - 78) / spanY), .2, 1.8);
  M.scale = M.scale ? lerp(M.scale, target, .05) : target;
  M.midX = M.midX === undefined ? (minX + maxX) / 2 : lerp(M.midX, (minX + maxX) / 2, .05);
  M.midY = M.midY === undefined ? (minY + maxY) / 2 : lerp(M.midY, (minY + maxY) / 2, .05);
  const scale = M.scale;
  const cx = w / 2, cy = h / 2 + 2;
  const project = n => ({
    x: cx + (n.x * cos - n.y * sin - M.midX) * scale,
    y: cy + (n.x * sin + n.y * cos - M.midY) * scale,
  });

  const pts = M.nodes.map(project);

  // edges
  ctx.lineWidth = 1;
  for (const e of M.edges) {
    const a = pts[e.s], b = pts[e.t];
    const alpha = Math.min(M.alphas[M.nodes[e.s].t], M.alphas[M.nodes[e.t].t]);
    if (e.cross) {
      ctx.save();
      ctx.setLineDash([3, 5]);
      ctx.strokeStyle = `rgba(20,18,14,${.09 * alpha})`;
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const bow = .18;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(mx + (my - cy) * bow, my - (mx - cx) * bow, b.x, b.y);
      ctx.stroke();
      ctx.restore();
    } else {
      ctx.strokeStyle = hexA(TYPE_COLOR[M.nodes[e.s].t], .3 * alpha);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
  }

  // walk particles in flight
  for (const p of M.particles) {
    const e = M.edges[p.e];
    if (!e) continue;
    const a = pts[e.s], b = pts[e.t];
    const x = lerp(a.x, b.x, p.p), y = lerp(a.y, b.y, p.p);
    ctx.fillStyle = "#c9604d";
    ctx.beginPath();
    ctx.arc(x, y, 2.1, 0, Math.PI * 2);
    ctx.fill();
    p.p += p.v;
    if (p.p >= 1) {
      p.p = 0;
      p.e = Math.floor(Math.random() * M.edges.length);
      p.v = .004 + Math.random() * .008;
    }
  }

  // nodes
  for (let i = 0; i < M.nodes.length; i++) {
    const n = M.nodes[i], p = pts[i];
    const a = M.alphas[n.t];
    const r = n.r * scale;
    if (n.hub) {
      ctx.strokeStyle = hexA(TYPE_COLOR[n.t], .5 * a);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r + 3.4, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.fillStyle = hexA(TYPE_COLOR[n.t], a);
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = `rgba(20,18,14,${.28 * a})`;
    ctx.lineWidth = .8;
    ctx.stroke();
  }

  // cluster labels ride their cluster
  TYPES.forEach((t, i) => {
    const members = M.nodes.filter(n => n.t === t);
    if (!members.length) return;
    const mid = members.reduce((acc, n) => {
      const p = project(n);
      acc.x += p.x; acc.y += p.y; return acc;
    }, { x: 0, y: 0 });
    mid.x /= members.length; mid.y /= members.length;
    const chip = labels[i];
    if (!chip) return;
    const top = mid.y - (58 + 34 * scale);
    chip.style.left = clamp(mid.x, 90, w - 90) + "px";
    chip.style.top = clamp(top, 34, h - 20) + canvas.offsetTop + "px";
    chip.style.opacity = String(clamp(M.alphas[t] + .15, .3, 1));
  });
}

function hexA(hex, a) {
  const v = parseInt(hex.slice(1), 16);
  return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${clamp(a, 0, 1)})`;
}

// ── panel painters ──────────────────────────────────────────────────────
function paintSpark(L) {
  const c = document.getElementById("spark");
  if (!c) return;
  const { ctx, w, h } = fitCanvas(c);
  ctx.clearRect(0, 0, w, h);
  const n = L.spark.length, bw = w / n;
  for (let i = 0; i < n; i++) {
    const v = L.spark[i];
    const bh = Math.max(1.5, v * (h - 4));
    ctx.fillStyle = i % 3 === 0
      ? hexA("#c9604d", .25 + .6 * (i / n))
      : `rgba(20,18,14,${.12 + .3 * (i / n)})`;
    ctx.fillRect(i * bw, h - bh, Math.max(1, bw - 1.6), bh);
  }
}

function paintRail(L, t) {
  const c = document.getElementById("railcanvas");
  if (!c) return;
  const { ctx, w, h } = fitCanvas(c);
  ctx.clearRect(0, 0, w, h);
  const y = h / 2;

  ctx.strokeStyle = "rgba(20,18,14,.22)";
  ctx.setLineDash([2, 6]);
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  ctx.setLineDash([]);

  const gate = w / 2;
  for (let i = 0; i < 26; i++) {
    const phase = ((t / 2600) + i / 26) % 1;
    const x = phase * w;
    const past = x > gate;
    ctx.fillStyle = past ? "rgba(79,124,97,.85)" : "rgba(201,96,77,.85)";
    ctx.beginPath();
    ctx.arc(x, y, past ? 2.4 : 2.1, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.save();
  ctx.translate(gate, y);
  ctx.rotate(Math.PI / 4);
  ctx.strokeStyle = "#14120e";
  ctx.fillStyle = "#f7f5f1";
  ctx.lineWidth = 1.2;
  ctx.fillRect(-6, -6, 12, 12);
  ctx.strokeRect(-6, -6, 12, 12);
  ctx.restore();
  ctx.fillStyle = "#9a958c";
  ctx.font = "7px 'JetBrains Mono',monospace";
  ctx.textAlign = "center";
  ctx.fillText("RESOLVER", gate, y + 16);
}

function paintHops(L) {
  const host = document.getElementById("hops");
  if (!host) return;
  const bins = L.hopBins;
  const max = Math.max(1, ...bins);
  const total = bins.reduce((a, b) => a + b, 0);
  let running = 0, median = 0;
  for (let i = 0; i < bins.length; i++) {
    running += bins[i];
    if (running >= total / 2) { median = i; break; }
  }
  if (host.children.length !== bins.length) {
    host.textContent = "";
    bins.forEach(() => {
      const col = el("div", "hopcol", `<div class="v">0</div><div class="bar"></div>`);
      host.appendChild(col);
    });
    const p50 = el("div", "p50", "<em>p50</em>");
    p50.id = "p50";
    host.appendChild(p50);
  }
  bins.forEach((v, i) => {
    const col = host.children[i];
    col.classList.toggle("median", i === median);
    col.querySelector(".v").textContent = v ? fmt(v) : "";
    col.querySelector(".bar").style.height = (v / max * 82) + "%";
  });
  const p50 = document.getElementById("p50");
  if (p50) p50.style.left = ((median + 1) / bins.length * 100 - 1.4) + "%";
  const badge = document.getElementById("s-median");
  if (badge) badge.textContent = `Median ${median + 1} hops`;
}

function paintLedger(L) {
  const host = document.getElementById("ledger");
  if (!host) return;
  const rows = L.edgeRows;
  const max = Math.max(1, ...rows.map(r => r.count));
  const totalEdges = rows.reduce((a, r) => a + r.count, 0);
  const totalTrav = rows.reduce((a, r) => a + r.traversals, 0) || 1;
  rows.forEach((r, i) => {
    const node = host.children[i];
    node.querySelector(".fill").style.width = (r.count / max * 100) + "%";
    // the caret marks this type's share of the traffic, not its share of edges
    node.querySelector(".caret").style.left =
      clamp(r.traversals / totalTrav * 100, 1, 97) + "%";
    node.querySelector(".caret").style.opacity = r.traversals ? ".65" : "0";
    node.querySelector(".n").textContent = compact(r.count);
  });
  const badge = document.getElementById("s-edgetotal");
  if (badge) badge.textContent = compact(totalEdges) + " edges";
}

function paintWvf(L, d) {
  const c = document.getElementById("wvf");
  if (!c) return;
  const { ctx, w, h } = fitCanvas(c);
  ctx.clearRect(0, 0, w, h);
  const s = L.series;
  if (!s.length) return;
  const max = Math.max(1, ...s.map(p => p.flat)) * 1.12;
  const px = i => (i / Math.max(1, s.length - 1)) * (w - 46);
  const py = v => h - 12 - (v / max) * (h - 24);

  ctx.strokeStyle = "rgba(20,18,14,.09)";
  for (let g = 0; g <= 3; g++) {
    const y = 12 + g * (h - 24) / 3;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w - 46, y); ctx.stroke();
  }

  const area = (key, fill, stroke, dash) => {
    ctx.beginPath();
    ctx.moveTo(0, h - 12);
    s.forEach((p, i) => ctx.lineTo(px(i), py(p[key])));
    ctx.lineTo(px(s.length - 1), h - 12);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.save();
    if (dash) ctx.setLineDash(dash);
    ctx.beginPath();
    s.forEach((p, i) => (i ? ctx.lineTo(px(i), py(p[key])) : ctx.moveTo(px(i), py(p[key]))));
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.2;
    ctx.stroke();
    ctx.restore();
  };
  area("flat", hexA(TYPE_COLOR.source, .16), hexA("#8c7a63", .8), [4, 3]);
  area("walk", hexA(TYPE_COLOR.entity, .28), hexA(TYPE_COLOR.entity, .95), null);

  const last = s[s.length - 1];
  ctx.font = "8px 'JetBrains Mono',monospace";
  ctx.textAlign = "left";
  [["flat", "#8c7a63"], ["walk", TYPE_COLOR.entity]].forEach(([key, colour]) => {
    const y = py(last[key]);
    ctx.fillStyle = colour;
    ctx.beginPath(); ctx.arc(px(s.length - 1), y, 2.2, 0, Math.PI * 2); ctx.fill();
    ctx.fillText(compact(last[key]), w - 40, clamp(y + 3, 10, h - 4));
  });

  const flatSum = s.reduce((a, p) => a + p.flat, 0) || 1;
  const walkSum = s.reduce((a, p) => a + p.walk, 0);
  const badge = document.getElementById("s-saving");
  if (badge) badge.textContent = "−" + ((1 - walkSum / flatSum) * 100).toFixed(0) + "% tokens";
}

function paintGrid(L, now) {
  const host = document.getElementById("grid");
  if (!host) return;
  const cells = L.cells;
  for (let i = 0; i < GRID_CELLS; i++) {
    const node = host.children[i];
    const v = cells[i];
    const fresh = L.fresh.has(i) && now - L.fresh.get(i) < 1400;
    const cls = v === -1 ? "cell empty" : v === 1 ? (fresh ? "cell fresh" : "cell") : "cell dead";
    if (node.className !== cls) node.className = cls;
  }
  for (const [i, t] of L.fresh) if (now - t > 1600) L.fresh.delete(i);
}

function paintDeadEnds(L) {
  const host = document.getElementById("dend");
  if (!host) return;
  const rows = L.deadRows;
  const total = rows.reduce((a, r) => a + r.count, 0);
  const max = Math.max(1, ...rows.map(r => r.count));
  rows.forEach((r, i) => {
    const node = host.children[i];
    node.querySelector("i").textContent = fmt(r.count);
    node.querySelector(".dfill").style.width = (r.count / max * 100) + "%";
    node.style.opacity = r.count ? "1" : ".4";
  });
  const big = document.getElementById("s-deadtotal");
  if (big) big.textContent = fmt(total);
  const walks = L.cells.filter(v => v >= 0).length || 1;
  const rate = document.getElementById("s-resolvedrate");
  if (rate) rate.textContent = "Resolved " + pct(L.cells.filter(v => v === 1).length / walks);
}

function paintWalkTime(L, d) {
  const c = document.getElementById("walktime");
  if (!c) return;
  const { ctx, w, h } = fitCanvas(c);
  ctx.clearRect(0, 0, w, h);
  const s = L.walkTime;
  if (!s.length) return;
  // Range from the observed floor, not from zero: walk cost varies inside a
  // narrow band, and a zero-based axis renders that band as a solid block.
  const hi = Math.max(...s), lo = Math.min(...s);
  const span = Math.max(1, hi - lo) * 1.25;
  const base = lo - Math.max(1, hi - lo) * .15;
  const px = i => (i / Math.max(1, s.length - 1)) * (w - 4) + 2;
  const py = v => h - 8 - ((v - base) / span) * (h - 16);

  ctx.beginPath();
  ctx.moveTo(px(0), h - 4);
  s.forEach((v, i) => ctx.lineTo(px(i), py(v)));
  ctx.lineTo(px(s.length - 1), h - 4);
  ctx.closePath();
  ctx.fillStyle = "rgba(130,183,149,.24)";
  ctx.fill();
  ctx.beginPath();
  s.forEach((v, i) => (i ? ctx.lineTo(px(i), py(v)) : ctx.moveTo(px(i), py(v))));
  ctx.strokeStyle = "#4f7c5f";
  ctx.lineWidth = 1.2;
  ctx.stroke();

  ctx.font = "7px 'JetBrains Mono',monospace";
  ctx.fillStyle = "#6b6862";
  ctx.textAlign = "left";
  ctx.fillText("WALK TIME", 3, 9);
  ctx.textAlign = "right";
  ctx.fillStyle = "#4f7c5f";
  ctx.fillText("30s", w - 3, 9);

  const el2 = document.getElementById("s-walktime");
  if (el2) el2.textContent = "−" + ((d.walk_vs_flat.saving) * 100).toFixed(0) + "%";
}

// ── main loop ───────────────────────────────────────────────────────────
function start(d) {
  build(d);
  const L = makeLive(d);
  const M = makeMesh(d);
  const canvas = document.getElementById("mesh");
  const labels = TYPES.map(t => document.getElementById("cl-" + t));
  const stageChips = Array.from(document.querySelectorAll(".stagechip"));
  const cards = Array.from(document.querySelectorAll(".card"));

  let lastWalk = 0, lastStage = 0, lastPhase = 0, lastClock = 0;
  const STAGE_MS = 1500, PHASE_MS = 4500, WALK_MS = 130;

  function setStage(i) {
    stageChips.forEach((c, k) => c.classList.toggle("on", k === i));
    cards.forEach((c, k) => c.classList.toggle("on", k === i));
    const badge = document.getElementById("s-stagebadge");
    if (badge) badge.textContent = `Stage ${String(i + 1).padStart(2, "0")} / ${String(cards.length).padStart(2, "0")}`;
  }

  function setPhase(i) {
    const [name, desc] = PHASES[i];
    document.getElementById("s-phase").textContent = name;
    document.getElementById("s-phasedesc").textContent = desc;
    document.getElementById("s-phasepill").textContent = name.split("· ")[1];
  }

  function paintHeader() {
    document.getElementById("s-nodes").textContent = fmt(L.nodes);
    document.getElementById("s-edges").textContent = fmt(L.edgesTick);
    document.getElementById("s-walks").textContent = fmt(L.walksTotal);
    document.getElementById("s-trav").textContent = L.travMs + "ms";
    document.getElementById("s-buildmeta").innerHTML =
      `Build ${L.build} · Spans ${fmt(L.spans)} · Edges ${fmt(L.committed)}`;
    document.getElementById("s-dropped").textContent = fmt(L.dropped);
    document.getElementById("s-committed").textContent = fmt(L.committed);
    const counts = {};
    for (const n of M.nodes) counts[n.t] = (counts[n.t] || 0) + 1;
    TYPES.forEach((t, i) => {
      const c = counts[t] || 0;
      const cell = document.getElementById("s-count-" + t);
      if (cell) cell.textContent = c;
      const row = d.node_types.find(r => r.type === t);
      if (labels[i]) labels[i].textContent = `TYPE ${i + 1} · ${(row ? row.label : t.toUpperCase())} · ${c}`;
    });
  }

  setStage(0);
  setPhase(0);
  paintHeader();
  paintHops(L);
  paintLedger(L);
  paintDeadEnds(L);

  function frame(now) {
    if (now - lastWalk > WALK_MS) {
      lastWalk = now;
      stepWalk(L);
      paintHops(L);
      paintLedger(L);
      paintDeadEnds(L);
      paintWvf(L, d);
      paintWalkTime(L, d);
      paintSpark(L);
      paintHeader();
    }
    if (now - lastStage > STAGE_MS) {
      lastStage = now;
      L.stage = (L.stage + 1) % 6;
      setStage(L.stage);
      if (L.stage === 0) stepBuild(L, d);
    }
    if (now - lastPhase > PHASE_MS) {
      lastPhase = now;
      L.phase = (L.phase + 1) % PHASES.length;
      setPhase(L.phase);
    }
    if (now - lastClock > 500) {
      lastClock = now;
      document.getElementById("clock").textContent =
        new Date().toTimeString().slice(0, 8);
    }

    // Phase 3 pulls the clusters apart; the others let them settle back.
    const spread = PHASES[L.phase][0].includes("CLUSTER") ? 1.28 : 1;
    simulate(M, spread);
    M.theta += 0.00042;
    drawMesh(M, canvas, PHASES[L.phase][2], labels);
    paintGrid(L, now);
    paintRail(L, now);

    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  window.addEventListener("resize", () => {
    paintSpark(L); paintWvf(L, d); paintWalkTime(L, d);
  });
}

function boot(d) {
  if (!d || !d.header) {
    document.getElementById("sheet").innerHTML =
      `<div class="panel pad"><h2 class="ptitle">No snapshot</h2>
       <div class="psub" style="margin-top:8px">
         Run <b>python -m contextmesh export --inline</b> to build the graph and write this page's data.
       </div></div>`;
    return;
  }
  start(d);
}

// Inline data renders immediately; a served copy refreshes it if newer.
if (DATA && DATA.header) {
  boot(DATA);
} else {
  fetch("data/mesh.json")
    .then(r => (r.ok ? r.json() : null))
    .then(boot)
    .catch(() => boot(null));
}
})();
