// The Coins Review and Wallets Review pages (ported from Rug Radar's index.html and wallet.html). Loaded after radar.js; uses el(), labPanel() and
// table() from index.html and radarCall(), rtag() and friends from radar.js, only inside functions.
'use strict';

const rrStyle = document.createElement('style');
rrStyle.textContent = `
.rr-search { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px } .rr-search input { flex: 1; min-width: 240px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--ink); font: inherit }
.rr-row { display: flex; gap: 10px 16px; flex-wrap: wrap; align-items: center; margin-bottom: 10px } .rr-row .grow { flex: 1 }
.rr-chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center } .rr-chip { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 999px; padding: 2px 10px; font: inherit; font-size: 12px; cursor: pointer }
.rr-chip.crit { border-color: var(--crit) } .rr-chip.warn { border-color: var(--warn) } .rr-chip.ok { border-color: var(--ok) }
.rr-notice { padding: 10px 12px; border-radius: 6px; margin-bottom: 10px; font-size: 13px; border: 1px solid var(--line); background: var(--info-soft) } .rr-notice.warn { background: var(--warn-soft); border-color: var(--warn) } .rr-notice.crit { background: var(--crit-soft); border-color: var(--crit) }
.rr-tokens { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 8px }
.rr-tok { width: 100%; display: flex; gap: 10px; align-items: center; text-align: left; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--ink); font: inherit; cursor: pointer }
.rr-tok img, .rr-tok .ph { width: 32px; height: 32px; border-radius: 50%; background: var(--row); flex: none } .rr-tok .s { display: block; font-weight: 700 } .rr-tok .nm { display: block; font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis }
.rr-tok .r { margin-left: auto; text-align: right; font-size: 12px; color: var(--muted); flex: none } .rr-tok .r b { display: block; color: var(--ink) }
.rr-hero { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 340px); gap: 16px; margin-bottom: 14px } @media (max-width: 760px) { .rr-hero { grid-template-columns: 1fr } }
.rr-ident { display: flex; gap: 12px; align-items: flex-start } .rr-ident img { width: 56px; height: 56px; border-radius: 50%; flex: none } .rr-ident h2 { margin: 0 } .rr-ident h2 span { color: var(--muted); font-weight: 400; font-size: 14px }
.rr-addrs { font-size: 12px; color: var(--muted); word-break: break-all } .rr-addrs code { font-family: var(--f-mono); font-size: 11.5px } .rr-links { display: flex; gap: 4px 12px; flex-wrap: wrap; margin: 6px 0; font-size: 12.5px }
.rr-verdicts { display: grid; gap: 8px } .rr-verdict { border: 1px solid var(--line); border-left-width: 4px; border-radius: 6px; padding: 10px 12px; background: var(--surface) } .rr-verdict .v { display: block; font-family: var(--f-display); font-size: 20px; font-weight: 700 } .rr-verdict p { margin: 4px 0 0; font-size: 12.5px; color: var(--muted) }
.sev-crit { border-color: var(--crit) } .sev-warn { border-color: var(--warn) } .sev-ok { border-color: var(--ok) } .sev-info { border-color: var(--line) }
.rr-gauge { display: flex; gap: 3px; margin: 4px 0 } .rr-gauge i { width: 22px; height: 6px; border-radius: 3px; background: var(--line) } .rr-gauge i.on { background: currentColor }
.sev-crit .rr-gauge, .sev-crit .v { color: var(--crit) } .sev-warn .rr-gauge, .sev-warn .v { color: var(--warn) } .sev-ok .rr-gauge, .sev-ok .v { color: var(--ok) }
.rr-tabs { display: flex; gap: 6px; margin: 8px 0 12px } .rr-tabs button { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 6px 6px 0 0; padding: 6px 14px; font: inherit; cursor: pointer } .rr-tabs button[aria-selected="true"] { background: var(--accent-soft); border-color: var(--accent); font-weight: 600 }
.rr-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 14px } .rr-metric { border: 1px solid var(--line); border-radius: 6px; background: var(--surface); padding: 8px 12px } .rr-metric .l { display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); font-weight: 600 } .rr-metric .v { display: block; font-family: var(--f-display); font-size: 20px; font-weight: 700 } .rr-metric .s { display: block; font-size: 12px; color: var(--muted) }
.rr-cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px } @media (max-width: 900px) { .rr-cols { grid-template-columns: 1fr } }
.rr-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px } .rr-list li { border: 1px solid var(--line); border-left-width: 4px; border-radius: 6px; padding: 7px 10px; background: var(--surface); display: grid; grid-template-columns: 1fr auto; gap: 2px 10px; font-size: 13px }
.rr-list .what, .rr-list .t { font-weight: 600 } .rr-list .why, .rr-list .d { grid-column: 1 / -1; color: var(--muted); font-size: 12px } .rr-list .res, .rr-list .m { font-family: var(--f-mono); font-size: 12px; text-align: right }
.rr-bars { display: grid; gap: 4px } .rr-bar { display: grid; grid-template-columns: 110px 1fr 56px; gap: 8px; align-items: center; font-size: 12px } .rr-bar .track { background: var(--row); border-radius: 3px; height: 10px } .rr-bar .fill { height: 100%; border-radius: 3px; background: var(--muted) } .rr-bar .fill.pool { background: var(--accent) } .rr-bar .fill.cluster { background: var(--warn) } .rr-bar .fill.insider { background: var(--crit) } .rr-bar .p { text-align: right; font-variant-numeric: tabular-nums }
.rr-legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 11.5px; color: var(--muted); margin-bottom: 6px }
.rr-facts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 10px } .rr-facts div { border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px } .rr-facts b { display: block; font-size: 16px } .rr-facts span { font-size: 11.5px; color: var(--muted) }
.rr-box { border: 1px solid var(--line); border-left-width: 4px; border-radius: 6px; padding: 8px 12px; margin-bottom: 8px; background: var(--surface) } .rr-box b { font-size: 18px; font-family: var(--f-display) } .rr-box p { margin: 4px 0 0; font-size: 12.5px; color: var(--muted) } .rr-box .label { display: block }
.rr-act { display: grid; gap: 6px } .rr-actrow { display: grid; grid-template-columns: 36px 1fr 200px; gap: 8px; align-items: center; font-size: 12.5px } .rr-split { display: flex; height: 10px; border-radius: 3px; overflow: hidden; background: var(--row) } .rr-split .b { background: var(--ok) } .rr-split .s { background: var(--crit) }
.rr-pbar { height: 8px; border-radius: 4px; background: var(--row); overflow: hidden; margin: 6px 0 } .rr-pbar div { height: 100%; background: var(--accent); width: 0; transition: width .4s } .rr-pbar.indet div { width: 30%; animation: rrslide 1.2s infinite ease-in-out } @keyframes rrslide { 0% { margin-left: 0 } 50% { margin-left: 70% } 100% { margin-left: 0 } }
.rr-group { border: 1px solid var(--line); border-left-width: 4px; border-radius: 6px; padding: 8px 12px; margin-bottom: 8px; background: var(--surface) } .rr-group h3 { margin: 0 0 4px; font-size: 14px } .rr-gstats { display: flex; gap: 4px 14px; flex-wrap: wrap; font-size: 12.5px } .rr-group p { margin: 4px 0; font-size: 12.5px; color: var(--muted) }
.pos { color: var(--ok) } .neg { color: var(--crit) } .rr-gtag { display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 0 7px; font-size: 11px; background: var(--surface); color: var(--ink); cursor: pointer; margin-right: 3px }
.rr-big { font-family: var(--f-display); font-size: 34px; font-weight: 700 } .rr-pill { display: inline-block; border: 1px solid; border-radius: 999px; padding: 2px 12px; font-weight: 600; margin-left: 10px; vertical-align: 6px }
.rr-pc { width: 100%; height: 240px; display: block } .rr-pc text { font-size: 10.5px; fill: var(--muted) } .rr-pc .grid { stroke: var(--line); stroke-width: 1 } .rr-pc .zero { stroke: var(--muted); stroke-width: 1 } .rr-pc .ln { fill: none; stroke-width: 2 } .rr-pc .ln.up { stroke: var(--ok) } .rr-pc .ln.down { stroke: var(--crit) }
.rr-hours { display: flex; align-items: flex-end; gap: 2px; height: 60px; margin: 4px 0 } .rr-hours i { flex: 1; background: var(--accent); border-radius: 2px 2px 0 0; min-height: 1px }
.rr-month { display: grid; grid-template-columns: 56px 1fr 1fr 80px; gap: 6px; align-items: center; font-size: 12.5px } .rr-month .l { text-align: right } .rr-month i { display: inline-block; height: 10px; background: var(--crit); border-radius: 2px } .rr-month .r i { background: var(--ok) } .rr-month .l i { float: right }
.rr-sum { display: flex; gap: 6px 18px; flex-wrap: wrap; font-size: 12.5px; margin-bottom: 8px }
`;
document.head.append(rrStyle);

const rrHash = re => { try { return decodeURIComponent((location.hash.match(re) || [])[1] || ''); } catch (e) { return ''; } };
const rrEsc = x => x;   // (all text goes through el() / textContent, so there is nothing to escape)
const rrMoney = (n, signed) => { if (n == null || isNaN(n)) return '—'; const a = Math.abs(n), s = a >= 1e6 ? (a / 1e6).toFixed(2) + 'M' : a >= 1e4 ? (a / 1e3).toFixed(1).replace(/\.0$/, '') + 'K' : Math.round(a).toLocaleString('en-US'); return (n < 0 ? '−' : signed && n > 0 ? '+' : '') + '$' + s; };
const rrPnl = n => el('span', { className: n >= 0 ? 'pos' : 'neg', textContent: rrMoney(n, true) });
const rrUsd = n => { if (n == null || isNaN(n)) return '—'; const a = Math.abs(n); if (a >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B'; if (a >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M'; if (a >= 1e4) return '$' + (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K'; if (a >= 1) return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 2 }); if (a === 0) return '$0'; return '$' + Number(n.toPrecision(4)).toString(); };
const rrPct = (n, d = 1) => n == null || isNaN(n) ? '—' : (n >= 0 ? '+' : '') + n.toFixed(d) + '%';
const rrPlain = (n, d = 1) => n == null || isNaN(n) ? '—' : n.toFixed(d) + '%';
const rrInt = n => n == null || isNaN(n) ? '—' : Math.round(n).toLocaleString('en-US');
const rrAge = ms => { if (!(ms > 0)) return '—'; const m = Math.floor(ms / 60000), h = Math.floor(m / 60), d = Math.floor(h / 24); return d >= 1 ? d + 'd ' + (h % 24) + 'h' : h >= 1 ? h + 'h ' + (m % 60) + 'm' : m + 'm'; };
const rrSafe = u => /^https?:\/\//i.test(String(u || ''));
const rrStore = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } };
const rrRecall = (k, d) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } };
const rrA = (href, text, title) => el('a', { href, target: '_blank', rel: 'noopener noreferrer', textContent: text, ...(title ? { title } : {}) });
const rrMetric = (l, v, s) => el('div', { className: 'rr-metric' }, el('span', { className: 'l', textContent: l }), el('span', { className: 'v' }, v), el('span', { className: 's' }, ...[].concat(s || '')));
const rrProgress = (box, p, startedAt, samples) => {   // stage text, bar and time left for a running job
  const now = Date.now(); box.stage.textContent = p.total ? `${p.stage}: ${p.done.toLocaleString()} of ${p.total.toLocaleString()}` : (p.stage || 'Working');
  box.bar.classList.toggle('indet', !p.total); box.bar.firstElementChild.style.width = p.total ? (p.done / p.total * 100).toFixed(1) + '%' : '';
  if (p.total) { if (samples.length && samples[0].stage !== p.stage) samples.length = 0; samples.push({ t: now, done: p.done, stage: p.stage }); while (samples.length > 2 && now - samples[0].t > 30000) samples.shift(); }
  const f = samples[0], rate = f && now > f.t + 3000 ? (p.done - f.done) / ((now - f.t) / 1000) : 0, mins = s => s < 90 ? Math.round(s) + ' s' : Math.round(s / 60) + ' min';
  box.eta.textContent = (startedAt ? `Running for ${mins((now - startedAt) / 1000)}.` : '') + (p.total && rate > 0 ? ` About ${mins((p.total - p.done) / rate)} left.` : '');
};
const rrProgressBox = (title, cancelFn) => { const stage = el('span', { textContent: 'Starting' }), bar = el('div', { className: 'rr-pbar indet' }, el('div')), eta = el('span', { className: 'small' }), cancel = el('button', { type: 'button', className: 'sm', textContent: 'Cancel', onclick: () => { cancel.disabled = true; cancelFn(); } });
  return { stage, bar, eta, cancel, node: el('section', { className: 'card lab' }, el('div', { className: 'lab-head' }, el('h2', { textContent: title })), el('div', {}, stage), bar, el('div', { style: 'display:flex;gap:12px;align-items:center' }, eta, cancel)) }; };

// ---- Coins ------------------------------------------------------------------------------------------------------------
const coin = { q: '', review: null, busy: false, notice: null, cands: null, tab: 'overview', tr: { mint: null, data: null, run: 0, timer: null, group: null, sort: 'pnlUsd', filter: 'all', status: 'idle', hint: '', progress: null, startedAt: 0, samples: [] }, timer: null, inited: false };

async function loadCoins() {
  const m = rrHash(/^#coins\/(.+)$/);
  if (!coin.inited) { coin.inited = true; renderCoins(); const start = m || (rrRecall('rr:recent', [])[0] || {}).mint; if (start) coinLookup(start); return; }
  if (m && (!coin.review || coin.review.token.mint !== m) && !coin.busy) coinLookup(m);
}
window.addEventListener('hashchange', () => { if (!$('tab-coins').hidden) loadCoins(); });

async function coinLookup(q, quiet) {
  q = String(q || '').trim(); if (!q || coin.busy) return;
  coin.busy = true; coin.q = q; if (!quiet) { coin.notice = { text: 'Checking…', sev: 'info' }; renderCoins(); }
  try {
    const out = await radarCall('/api/radar/coins/review?q=' + encodeURIComponent(q));
    if (out.candidates) { coin.cands = out.candidates; coin.review = null; coin.notice = { text: "Tickers aren't unique. Many copycats share a name, so check the mint address before you buy.", sev: 'info' }; coin.busy = false; renderCoins(); return; }
    coin.cands = null; coin.review = out.review; const pr = out.review.pair;
    const msg = [out.rugError ? `RugCheck didn't answer (${out.rugError}). Contract and holder checks are missing from this review.` : '',
      pr.dexScreener ? `DexScreener ${pr.dexScreener.missing ? "doesn't price this coin's main pool" : `shows a smaller main pool (${rrUsd(pr.dexScreener.liq)} liquidity, price ${rrUsd(pr.dexScreener.price)})`}. GeckoTerminal lists a deeper ${pr.quote ? '$' + pr.quote + ' ' : ''}pool.` : '',
      out.geckoError ? `GeckoTerminal didn't answer (${out.geckoError}). Market data is from DexScreener alone, which sometimes misses a coin's main pool.` : '',
      !out.review.sources.dexscreener ? "DexScreener didn't answer, so market data comes from GeckoTerminal alone." : '',
      out.review.priceCheck ? (out.review.priceCheck.allOff ? "Every pool's price is more than 2× off Jupiter's price. Check the price before trusting this review." : out.review.priceCheck.ignoredCount ? `Left out ${out.review.priceCheck.ignoredCount} pool(s) priced more than 2× off Jupiter's price.` : '') : ''].filter(Boolean).join(' ');
    coin.notice = msg ? { text: msg, sev: out.rugError || out.review.priceCheck && out.review.priceCheck.allOff ? 'warn' : 'info' } : null;
    const list = rrRecall('rr:recent', []).filter(x => x.mint !== out.review.token.mint); list.unshift({ mint: out.review.token.mint, symbol: out.review.token.symbol, sev: out.review.market.sev }); rrStore('rr:recent', list.slice(0, 8));
    if (location.hash !== '#coins/' + out.review.token.mint) history.replaceState(null, '', '#coins/' + out.review.token.mint);
    coinTradersFor(out.review.token.mint);
  } catch (e) { if (quiet) coin.notice = { text: 'Refresh failed: ' + e.message, sev: 'warn' }; else coin.notice = { text: e.message, sev: 'crit' }; }
  coin.busy = false; renderCoins();
}


function tokenButton(c) {
  const ch = c.change24h;
  return el('li', {}, el('button', { type: 'button', className: 'rr-tok', onclick: () => { $('rrq') && ($('rrq').value = c.mint); coinLookup(c.mint); scrollTo({ top: 0, behavior: 'smooth' }); } },
    c.image && rrSafe(c.image) ? el('img', { src: c.image, alt: '', loading: 'lazy' }) : el('span', { className: 'ph' }),
    el('span', { style: 'min-width:0' }, el('span', { className: 's', textContent: '$' + (c.symbol || '') }), el('span', { className: 'nm', textContent: `${c.name || ''} · ${shortAddr(c.mint)}` })),
    el('span', { className: 'r' }, el('b', { textContent: rrUsd(c.marketCap) }), el('span', { className: ch == null ? '' : ch >= 0 ? 'pos' : 'neg', textContent: rrPct(ch, 0) }), ' · ' + (c.volume24h != null ? 'vol ' + rrUsd(c.volume24h) : 'liq ' + rrUsd(c.liquidity)))));
}

function renderCoins() {
  const root = $('coins'), out = [];
  const input = el('input', { type: 'text', id: 'rrq', placeholder: 'Mint address, pair address, DexScreener link or $TICKER', autocomplete: 'off', spellcheck: false, value: coin.q, 'aria-label': 'Token to review' });
  const go = el('button', { type: 'submit', className: 'primary', textContent: coin.busy ? 'Checking…' : 'Review', disabled: coin.busy });
  const form = el('form', { className: 'rr-search' }, input, go); form.onsubmit = e => { e.preventDefault(); coinLookup(input.value); };
  const auto = el('input', { type: 'checkbox', checked: !!coin.timer }); auto.onchange = () => { clearInterval(coin.timer); coin.timer = null; if (auto.checked) coin.timer = setInterval(() => { if (coin.review && !document.hidden && !$('tab-coins').hidden) coinLookup(coin.review.token.mint, true); }, 30000); renderCoins(); };
  const recent = rrRecall('rr:recent', []);
  out.push(form, el('div', { className: 'rr-row' }, el('div', { className: 'rr-chips grow' }, ...(recent.length ? [el('span', { className: 'small', textContent: 'Recent:' }), ...recent.map(x => el('button', { type: 'button', className: 'rr-chip ' + x.sev, textContent: '$' + x.symbol, onclick: () => { input.value = x.mint; coinLookup(x.mint); } }))] : [])),
    el('label', { className: 'chk small' }, auto, ' Refresh every 30 s')));
  if (coin.notice) out.push(el('div', { className: 'rr-notice ' + coin.notice.sev, role: 'status', textContent: coin.notice.text }));
  if (coin.cands) out.push(labPanel(`${coin.cands.length} Solana tokens match "${coin.q}"`, 'Sorted by 24h volume', [], el('ul', { className: 'rr-tokens' }, ...coin.cands.map(tokenButton))));
  if (!coin.review && !coin.cands) out.push(el('p', { className: 'small', textContent: 'Paste a mint address, a DexScreener link or a ticker. It pulls live data from DexScreener, GeckoTerminal, RugCheck and Jupiter and scores the contract, the holders and the trading.' }));
  if (coin.review) out.push(...coinReport(coin.review));
  out.push(el('p', { className: 'small', textContent: 'Scores come from live data of the DexScreener, GeckoTerminal, RugCheck and Jupiter public APIs, using fixed thresholds. The main pool is the deepest one either DexScreener or GeckoTerminal lists. Constant-product pool math is an estimate: real fills also depend on fees, other pools and routing. Meme coins can lose most of their value within minutes; this is not advice.' }));
  const focus = document.activeElement && root.contains(document.activeElement) && document.activeElement.tagName === 'INPUT' && document.activeElement.id !== 'rrq';
  if (!focus) root.replaceChildren(...out);
  if (input.isConnected === false && coin.q === '') { /* first draw */ }
}

function coinReport(a) {
  const out = [], cls = n => n == null ? '' : n >= 0 ? 'pos' : 'neg';
  const m = a.token.mint, srcName = a.pair.source === 'geckoterminal' ? 'GeckoTerminal' : 'DexScreener';
  const links = [rrA('https://dexscreener.com/solana/' + m, 'DexScreener'), ...((/^pump(fun|swap)$/i.test(a.pair.dexId || '') || m.endsWith('pump')) ? [rrA('https://pump.fun/coin/' + m, 'pump.fun')] : []),
    rrA(a.pair.source === 'geckoterminal' && rrSafe(a.pair.url) ? a.pair.url : 'https://www.geckoterminal.com/solana/tokens/' + m, 'GeckoTerminal'), rrA('https://rugcheck.xyz/tokens/' + m, 'RugCheck'), rrA('https://solscan.io/token/' + m, 'Solscan'), rrA('https://app.bubblemaps.io/sol/token/' + m, 'Bubblemaps'),
    ...(a.links.websites || []).filter(w => rrSafe(w.url)).map(w => rrA(w.url, w.label || 'Website')), ...(a.links.socials || []).filter(s => rrSafe(s.url)).map(s => rrA(s.url, s.type === 'twitter' ? 'X' : s.type))];
  const gauge = n => el('div', { className: 'rr-gauge' }, ...[1, 2, 3, 4].map(i => el('i', { className: i <= n ? 'on' : '' })));
  const passing = a.checks.filter(c => c.sev === 'ok').length;
  out.push(el('section', { className: 'card lab' }, el('div', { className: 'rr-hero' },
    el('div', {}, el('div', { className: 'rr-ident' }, ...(a.token.image && rrSafe(a.token.image) ? [el('img', { src: a.token.image, alt: '' })] : []),
      el('div', {}, el('h2', {}, '$' + a.token.symbol + ' ', el('span', { textContent: a.token.name || '' })),
        el('div', { className: 'rr-addrs' }, el('div', {}, 'Mint ', el('code', { textContent: m })), el('div', {}, 'Main pool ', el('code', { textContent: a.pair.address || '' }), ` on ${a.pair.dexId}${a.pair.quote ? ' vs $' + a.pair.quote : ''}${a.pair.count > 1 ? ` · ${a.pair.count} pools` : ''} · market data from ${srcName}`)),
        el('div', { className: 'rr-links' }, ...links))),
      el('p', { style: 'margin:10px 0 0', textContent: a.summary })),
    el('div', { className: 'rr-verdicts' },
      el('div', { className: 'rr-verdict sev-' + a.contract.sev }, el('span', { className: 'label small', textContent: 'Contract risk' }), el('span', { className: 'v', textContent: a.contract.v }), gauge(a.contract.lvl), el('p', { textContent: a.hasRug ? `${passing} of ${a.checks.length} checks pass. RugCheck score ${a.rug.scoreNormalised ?? a.rug.score ?? '—'}.` : 'RugCheck data missing.' })),
      el('div', { className: 'rr-verdict sev-' + a.market.sev }, el('span', { className: 'label small', textContent: 'Market & dump risk' }), el('span', { className: 'v', textContent: a.market.v }), gauge(a.market.lvl), el('p', { textContent: `${a.flags.filter(f => f.sev === 'crit').length} critical, ${a.flags.filter(f => f.sev === 'warn').length} warning flags.` }))))));
  const tabBtn = (id, label) => el('button', { type: 'button', role: 'tab', 'aria-selected': String(coin.tab === id), textContent: label, onclick: () => { coin.tab = id; renderCoins(); } });
  out.push(el('div', { className: 'rr-tabs', role: 'tablist' }, tabBtn('overview', 'Overview'), tabBtn('traders', 'Top traders')));
  if (coin.tab === 'traders') { out.push(...coinTraders(a)); return out; }
  const sp = (v, t) => el('span', { className: cls(v), textContent: t });
  out.push(el('div', { className: 'rr-strip' },
    rrMetric('Price', rrUsd(a.price), [sp(a.ch.h1, rrPct(a.ch.h1)), ' 1h · ', sp(a.ch.m5, rrPct(a.ch.m5)), ' 5m']), rrMetric('Market cap', rrUsd(a.mcap), `FDV ${rrUsd(a.fdv)}`),
    rrMetric('Liquidity', rrUsd(a.liq), `${rrUsd(a.totalLiq)} all pools`), rrMetric('Volume 24h', rrUsd(a.vol24), a.pair.count > 1 ? `${rrUsd(a.vol.h24)} main pool` : `${rrUsd(a.vol.h1)} last hour`),
    rrMetric('Pair age', rrAge(a.ageMs), a.pair.createdAt ? 'since ' + new Date(a.pair.createdAt).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''), rrMetric('Holders', rrInt(a.rug.totalHolders), [sp(a.ch.h24, rrPct(a.ch.h24, 0)), ' 24h'])));
  const icon = { ok: '✓', warn: '!', crit: '✕', info: 'i' }, cnt = s => a.flags.filter(f => f.sev === s).length;
  out.push(el('div', { className: 'rr-cols' },
    labPanel('Contract checks', 'Can the dev rug it?', [], el('ul', { className: 'rr-list' }, ...a.checks.map(c => el('li', { className: 'sev-' + c.sev }, el('span', { className: 'what', textContent: icon[c.sev] + ' ' + c.what }), el('span', { className: 'res', textContent: c.res }), el('span', { className: 'why', textContent: c.why }))))),
    labPanel('Market red flags', [['crit', 'critical'], ['warn', 'warning'], ['ok', 'clear']].filter(([s]) => cnt(s)).map(([s, l]) => `${cnt(s)} ${l}`).join(' · '), [], el('ul', { className: 'rr-list' }, ...a.flags.map(f => el('li', { className: 'sev-' + f.sev }, el('span', { className: 't', textContent: f.t }), el('span', { className: 'm', textContent: f.m }), el('span', { className: 'd', textContent: f.d })))))));
  const hs = a.holders.slice(0, 16), maxP = Math.max(...hs.map(h => h.pct), 1);
  const t = a.trade, ds = a.dumpAll <= -50 ? 'crit' : a.dumpAll <= -25 ? 'warn' : 'ok';
  const tradeBox = !t ? null : (() => { const ts = t.error ? 'info' : t.noRoute || !t.sell || t.roundTripLossPct >= 20 ? 'crit' : t.roundTripLossPct >= 5 ? 'warn' : 'ok';
    return el('div', { className: 'rr-box sev-' + ts }, el('span', { className: 'label small', textContent: `Buy ${t.sizeSol} SOL and sell it straight back (Jupiter)` }), ...(t.error ? [el('b', { textContent: 'Not checked' }), el('p', { textContent: t.error })] : t.noRoute ? [el('b', { textContent: 'No route' }), el('p', { textContent: "Jupiter can't find a way to buy this coin." })] : !t.sell ? [el('b', { textContent: "Can't sell" }), el('p', { textContent: 'Jupiter can buy it but found no route to sell it back.' })] :
      [el('b', { textContent: t.roundTripLossPct < 0.05 ? 'No loss' : '−' + rrPlain(t.roundTripLossPct, 1) }), el('p', { textContent: `You'd get back ${t.sell.outSol.toFixed(3)} SOL. Buy impact ${rrPlain(t.buy.impactPct, 2)} via ${t.buy.route.join(' → ') || '—'}; sell impact ${rrPlain(t.sell.impactPct, 2)}. Fees included, across every pool Jupiter can reach.` })])); })();
  out.push(el('div', { className: 'rr-cols' },
    labPanel('Top holders', '% of supply', [], el('div', { className: 'rr-legend' }, el('span', { textContent: '■ Liquidity pool' }), el('span', { textContent: '■ Look-alike wallet sizes' }), el('span', { textContent: '■ Flagged insider' })),
      el('div', { className: 'rr-bars' }, ...(hs.length ? hs.map(h => el('div', { className: 'rr-bar' }, rrA('https://solscan.io/account/' + h.owner, (h.pool ? 'Pool · ' : '') + shortAddr(h.owner), h.owner), el('div', { className: 'track' }, el('div', { className: 'fill ' + (h.pool ? 'pool' : h.insider ? 'insider' : h.cluster ? 'cluster' : ''), style: `width:${(h.pct / maxP * 100).toFixed(1)}%` })), el('span', { className: 'p', textContent: h.pct.toFixed(2) + '%' }))) : [el('p', { className: 'small', textContent: 'No holder list available.' })])),
      el('div', { className: 'rr-facts' }, el('div', {}, el('b', { textContent: a.hasRug ? rrPlain(a.top10, 1) : '—' }), el('span', { textContent: 'Top 10 wallets' })), el('div', {}, el('b', { textContent: a.cluster.n >= 5 ? a.cluster.n : 'None' }), el('span', { textContent: 'Look-alike wallets' })),
        el('div', {}, el('b', { textContent: a.nets ? rrPlain(a.insPct, 1) : 'None' }), el('span', { textContent: 'Insider network' + (a.nets ? ` (${a.insWallets} wallets)` : '') })))),
    labPanel('Exit math', 'Jupiter quote, then main-pool estimate', [], ...(tradeBox ? [tradeBox] : []),
      a.nets ? el('div', { className: 'rr-box sev-' + ds }, el('span', { className: 'label small', textContent: 'If the insider network sells everything' }), el('b', { textContent: rrPct(a.dumpMain, 0) }), el('p', { textContent: `${rrInt(a.insTokens)} tokens (${rrUsd(a.insUsd)}) into a pool holding ${rrInt(a.liqBase)}. Spread across all pools: about ${rrPct(a.dumpAll, 0)}.` }))
        : el('div', { className: 'rr-box sev-info' }, el('span', { className: 'label small', textContent: 'Insider network' }), el('b', { textContent: 'None found' }), el('p', { textContent: a.hasRug ? 'RugCheck found no linked insider wallets. Still check the top holders.' : 'Needs RugCheck data.' })),
      a.impact.length ? table(['Market buy', 'Price moves', 'Share of pool', 'Impact'], a.impact.map(i => [rrUsd(i.size), rrPct(i.move, i.move < 1 ? 2 : 1), rrPlain(i.share, 2), el('td', {}, rtag(i.label, i.sev === 'crit' ? 'n' : i.sev === 'warn' ? 'w' : 'p'))])) : el('p', { className: 'small', textContent: 'No pool liquidity data.' }),
      el('p', { className: 'small', textContent: a.safeOrder ? `Keep single orders under about ${rrUsd(a.safeOrder)} to stay below 2% impact. The pool holds ${rrUsd(a.quoteSide)} on each side. Selling out has the same impact in the other direction.` : '' }))));
  out.push(labPanel('Trading activity', 'Main pool, buys vs sells', [], el('div', { className: 'rr-act' }, ...[['5m', 'm5'], ['1h', 'h1'], ['6h', 'h6'], ['24h', 'h24']].map(([l, k]) => { const b = (a.tx[k] || {}).buys || 0, s = (a.tx[k] || {}).sells || 0, tt = b + s || 1;
    return el('div', { className: 'rr-actrow' }, el('span', { textContent: l }), el('div', { className: 'rr-split', title: `${rrInt(b)} buys, ${rrInt(s)} sells` }, el('div', { className: 'b', style: `width:${(b / tt * 100).toFixed(1)}%` }), el('div', { className: 's', style: `width:${(s / tt * 100).toFixed(1)}%` })), el('span', { textContent: `${rrInt(b)} / ${rrInt(s)} · ${rrUsd(a.vol[k])}` })); })),
    el('p', { className: 'small', textContent: 'Green is buys, red is sells. Each row shows buys / sells · volume.' })));
  return out;
}

// ---- Top traders (a heavy job run by jobs.py) ---------------------------------------------------------------------------
function coinTradersFor(mint) {
  const T = coin.tr; if (mint === T.mint) return;
  Object.assign(T, { mint, data: null, group: null, status: 'idle', hint: '', progress: null, run: T.run + 1 }); clearTimeout(T.timer);
  radarCall(`/api/radar/traders?mint=${encodeURIComponent(mint)}&peek=1`).then(r => { if (mint !== T.mint) return; if (r.status === 'done') { T.data = r.result; T.status = 'done'; } else if (r.status === 'running') coinTradersPoll(mint, T.run, true); if (coin.tab === 'traders') renderCoins(); }).catch(() => {});
}
function coinTradersStart(refresh) { const T = coin.tr; if (!T.mint) return; T.run++; T.samples = []; T.status = 'running'; T.progress = { stage: 'Starting' }; renderCoins(); coinTradersPoll(T.mint, T.run, false, refresh); }
async function coinTradersPoll(mint, run, peek, refresh) {
  const T = coin.tr; if (run !== T.run) return;
  try {
    const b = await radarCall(`/api/radar/traders?mint=${encodeURIComponent(mint)}${refresh ? '&refresh=1' : ''}${peek ? '&peek=1' : ''}`); if (run !== T.run) return;
    if (b.status === 'none') throw new Error('The analysis stopped, probably because the server restarted. Start it again.');
    if (b.status === 'running') { T.status = 'running'; T.progress = b.progress || {}; T.startedAt = b.startedAt; if (coin.tab === 'traders') renderCoins(); T.timer = setTimeout(() => coinTradersPoll(mint, run, true), 1500); return; }
    if (b.status === 'error') { T.status = 'idle'; T.hint = b.cancelled ? 'Analysis cancelled.' : b.error; } else { T.status = 'done'; T.data = b.result; T.hint = ''; }
  } catch (e) { if (run !== T.run) return; T.status = 'idle'; T.hint = e.message; }
  if (coin.tab === 'traders') renderCoins();
}
function coinTraders(a) {
  const T = coin.tr, out = [];
  if (T.status === 'idle') {
    out.push(labPanel('Who made money on this coin?', '', [], el('p', { className: 'small', textContent: "Downloads every trade since launch and works out each wallet's profit, then looks for insider groups: the launch bundle, wallets that split a bag across many fresh wallets, clusters of same-size holders, and wallets funded by the same source. It needs a fast RPC (set it on the Settings page): about a minute per 3,000 transactions the first time; a repeat is faster." }),
      el('div', { style: 'display:flex;gap:12px;align-items:center' }, el('button', { type: 'button', className: 'primary', textContent: 'Analyze top traders', onclick: () => coinTradersStart(false) }), el('span', { className: 'small n', textContent: T.hint }))));
    return out;
  }
  if (T.status === 'running') {
    const box = rrProgressBox('Analyzing', async () => { try { await radarCall('/api/radar/traders/cancel', { mint: T.mint }); } catch (e) { /* ignore */ } }); rrProgress(box, T.progress || {}, T.startedAt, T.samples); box.cancel.disabled = (T.progress || {}).stage === 'Cancelling'; out.push(box.node); return out;
  }
  const r = T.data; if (!r) return out;
  const tagged = new Set(r.traders.filter(t => (t.tags || []).length).map(t => t.owner)), o = r.totals.outsiders, GS = { launch: 'crit', distribution: 'crit', cluster: 'warn', funder: 'warn' };
  out.push(el('div', { className: 'rr-strip' }, rrMetric('Traders', r.totals.traders.toLocaleString(), `${Math.round(r.totals.profitable / Math.max(1, r.totals.traders) * 100)}% in profit on paper`),
    rrMetric('Insider groups', String(r.groups.length), r.groups.length ? `${tagged.size} of the top ${r.traders.length} wallets are in one` : 'none found'),
    rrMetric('Outsiders: profit taken', rrMoney(o.realizedUsd, true), `${o.count.toLocaleString()} wallets outside the groups`), rrMetric('Outsiders: still held', rrMoney(o.heldUsd), `pool liquidity is ${rrMoney(r.liquidityUsd)}`)));
  out.push(labPanel('Insider groups', 'Biggest profit taken first', [], ...(r.groups.length ? r.groups.map(g => el('div', { className: 'rr-group sev-' + (GS[g.kind] || 'info') }, el('h3', { textContent: g.label }),
    el('div', { className: 'rr-gstats' }, el('span', {}, el('b', { textContent: g.count }), ' wallets'), el('span', {}, 'taken ', rrPnl(g.realizedUsd)), el('span', {}, 'held ', el('b', { textContent: rrMoney(g.heldUsd) })), el('span', { textContent: `in ${rrMoney(g.spentUsd)} → out ${rrMoney(g.gotUsd)}` })),
    el('p', { textContent: g.note }), el('button', { type: 'button', className: 'sm', textContent: 'Show these wallets', onclick: () => { T.group = T.group === g.id ? null : g.id; renderCoins(); } }))) : [el('p', { className: 'small', textContent: 'No launch bundle, bag-splitting, same-size clusters or shared funders found among the top wallets.' })])));
  out.push(labPanel('Launch block', r.launch.time ? new Date(r.launch.time * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '', [], r.launch.sequence.length ? el('ol', { className: 'small' }, ...r.launch.sequence.map(s => el('li', {}, 'Paid by ', el('code', { textContent: shortAddr(s.payer), title: s.payer }), s.payer === r.launch.creator ? ' (creator): ' : ': ',
    ...s.moves.flatMap((mv, i) => [i ? ', ' : '', el('code', { textContent: mv.owner === r.launch.curve ? 'bonding curve' : mv.pool ? 'pool' : shortAddr(mv.owner), title: mv.owner }), ` ${mv.delta >= 0 ? '+' : '−'}${(Math.abs(mv.delta) / 1e6).toFixed(1)}M`])))) : el('p', { className: 'small', textContent: 'No token movements in the launch block.' })));
  const byId = Object.fromEntries(r.groups.map(g => [g.id, g]));
  let list = r.traders.slice(); if (T.group) list = list.filter(t => (t.tags || []).includes(T.group)); else if (T.filter === 'outsiders') list = list.filter(t => !(t.tags || []).length);
  list.sort((x, y) => y[T.sort] - x[T.sort]);
  const fsel = el('select', {}, el('option', { value: 'all', textContent: 'Everyone', selected: T.filter === 'all' }), el('option', { value: 'outsiders', textContent: 'Outside insider groups', selected: T.filter === 'outsiders' })); fsel.onchange = () => { T.filter = fsel.value; T.group = null; renderCoins(); };
  const ssel = el('select', {}, ...[['pnlUsd', 'Total profit'], ['realizedUsd', 'Profit taken'], ['heldUsd', 'Still held']].map(([v, l]) => el('option', { value: v, textContent: l, selected: T.sort === v }))); ssel.onchange = () => { T.sort = ssel.value; renderCoins(); };
  out.push(labPanel('Top traders', T.group ? 'Showing one group' : '', [el('label', { className: 'chk' }, 'Show ', fsel), el('label', { className: 'chk' }, 'Sort by ', ssel)],
    table(['No.', 'Wallet', 'Total profit', 'Taken', 'Still held', 'In → out', 'Trades', 'Entry', 'Groups'], list.slice(0, 100).map((t, i) => [i + 1,
      el('td', {}, el('a', { href: '#wallets/' + t.owner, textContent: shortAddr(t.owner), title: 'Review this wallet on the Wallets Review page' }), ' ', rrA('https://solscan.io/account/' + t.owner, '↗'), t.owner === r.launch.creator ? ' creator' : ''), el('td', {}, rrPnl(t.pnlUsd)), el('td', {}, rrPnl(t.realizedUsd)),
      t.heldUsd >= 1 ? rrMoney(t.heldUsd) : '—', `${rrMoney(t.spentUsd)} → ${rrMoney(t.gotUsd)}`, `${t.buys}b / ${t.sells}s${t.tokIn > 0 ? ' +free' : ''}`,
      t.entryMinutes != null ? (t.entryMinutes < 1 ? '<1m' : t.entryMinutes < 120 ? Math.round(t.entryMinutes) + 'm' : (t.entryMinutes / 60).toFixed(1) + 'h') : '—',
      el('td', {}, ...(t.tags || []).map(id => el('button', { type: 'button', className: 'rr-gtag', title: (byId[id] || {}).label || id, textContent: (byId[id] || {}).kind === 'funder' ? 'funder ' + String((byId[id] || {}).funder || '').slice(0, 4) : (byId[id] || {}).kind === 'cluster' ? 'same size' : (byId[id] || {}).kind === 'distribution' ? 'split bag' : 'launch', onclick: () => { T.group = T.group === id ? null : id; renderCoins(); } })))])),
    el('p', { className: 'small', textContent: `Based on ${r.txCount.toLocaleString()} transactions since launch, analyzed ${new Date(r.generatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}. Profit = what a wallet got back minus what it paid (fees included), plus unsold tokens at today's price. Tokens a wallet received without paying count as free, so bag-splitting recipients look more profitable than they are.` + ((r.notes || []).length ? ' ' + r.notes.join(' ') : '') }),
    el('button', { type: 'button', className: 'sm', textContent: 'Run again', onclick: () => coinTradersStart(true) })));
  return out;
}

// ---- Wallets ----------------------------------------------------------------------------------------------------------
const wal = { q: '', days: [7, 30].includes(rrRecall('rr:wdays', 30)) ? rrRecall('rr:wdays', 30) : 30, /* 7 or 30 days: an older saved 90 / 180 / 365 becomes 30 */ all: false, data: null, status: 'idle', progress: null, startedAt: 0, samples: [], key: 0, timer: null, notice: null, saved: [], inited: false, sort: 'total', dir: -1, filter: '', showAll: false, side: '', rt: 25, range: rrRecall('rr:pcrange', 0) };
const walKey = () => `address=${encodeURIComponent(wal.q)}&days=${wal.days}${wal.all ? '&allTransfers=1' : ''}`;

async function loadWallets() {
  const h = rrHash(/^#wallets\/(.+)$/).split('/')[0];
  if (!wal.inited) { wal.inited = true; try { wal.saved = (await radarCall('/api/radar/bookmarks')).wallets; } catch (e) { /* ignore */ } renderWallets(); }
  if (h && h !== wal.q && wal.status !== 'running') { wal.q = h; walletStart(false); }
}
window.addEventListener('hashchange', () => { if (!$('tab-wallets').hidden) loadWallets(); });

function walletStart(refresh) {
  const a = wal.q.trim(); if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(a)) { wal.notice = { text: "That doesn't look like a Solana wallet address. It should be 32–44 letters and numbers.", sev: 'crit' }; renderWallets(); return; }
  clearTimeout(wal.timer); wal.key++; wal.notice = null; wal.q = a; wal.status = 'running'; wal.progress = { stage: 'Starting' }; wal.samples = []; if (wal.data && (wal.data.wallet !== a || wal.data.days !== wal.days)) wal.data = null;
  history.replaceState(null, '', '#wallets/' + a); renderWallets(); walletPoll(wal.key, refresh, false);
}
async function walletPoll(key, refresh, peek) {
  if (key !== wal.key) return;
  try {
    const b = await radarCall(`/api/radar/wallet?${walKey()}${refresh ? '&refresh=1' : ''}${peek ? '&peek=1' : ''}`); if (key !== wal.key) return;
    if (b.status === 'none') throw new Error('The review stopped, probably because the server restarted. Press Review wallet to start it again.');
    if (b.status === 'running') { wal.status = 'running'; wal.progress = b.progress || {}; wal.startedAt = b.startedAt; renderWallets(); wal.timer = setTimeout(() => walletPoll(key, false, true), 1500); return; }
    wal.status = 'idle';
    if (b.status === 'error') { if (b.cancelled) { wal.notice = { text: 'Review cancelled.', sev: 'info' }; } else throw new Error(b.error); } else {
      wal.data = b.result; wal.rt = 25; wal.showAll = false; wal.notice = (b.result.notes || []).length ? { text: b.result.notes.join(' '), sev: 'warn' } : null;
      const list = rrRecall('rr:wallets', []).filter(x => x.wallet !== b.result.wallet); list.unshift({ wallet: b.result.wallet, days: b.result.days, sev: b.result.summary.verdict.sev }); rrStore('rr:wallets', list.slice(0, 8)); }
  } catch (e) { if (key !== wal.key) return; wal.status = 'idle'; wal.notice = { text: e.message, sev: 'crit' }; }
  renderWallets();
}

function renderWallets() {
  const root = $('walletsPage'), out = [];
  const input = el('input', { type: 'text', id: 'rrw', placeholder: 'Wallet address', autocomplete: 'off', spellcheck: false, value: wal.q, 'aria-label': 'Wallet address' });
  const days = el('select', { 'aria-label': 'Window' }, ...[[7, 'Last 7 days'], [30, 'Last 30 days']].map(([v, l]) => el('option', { value: v, textContent: l, selected: Number(wal.days) === v })));
  const all = el('input', { type: 'checkbox', checked: wal.all });
  const go = el('button', { type: 'submit', className: 'primary', textContent: 'Review wallet', disabled: wal.status === 'running' });
  const form = el('form', { className: 'rr-search' }, input, days, go); form.onsubmit = e => { e.preventDefault(); wal.q = input.value.trim(); wal.days = Number(days.value); rrStore('rr:wdays', wal.days); wal.all = all.checked; walletStart(false); };
  const recent = rrRecall('rr:wallets', []);
  out.push(form, el('div', { className: 'rr-row' }, el('div', { className: 'rr-chips grow' }, ...(recent.length ? [el('span', { className: 'small', textContent: 'Recent:' }), ...recent.map(x => el('button', { type: 'button', className: 'rr-chip ' + x.sev, textContent: shortAddr(x.wallet), onclick: () => { wal.q = x.wallet; wal.days = [7, 30].includes(x.days) ? x.days : 30; walletStart(false); } }))] : [])),
    el('label', { className: 'chk small', title: 'Busy wallets receive thousands of small payments they did not sign; those are skipped unless this is ticked' }, all, ' Include every incoming transfer')));
  if (wal.notice) out.push(el('div', { className: 'rr-notice ' + wal.notice.sev, role: 'status', textContent: wal.notice.text }));
  // saved wallets
  const savedRows = wal.saved.map(b => [el('td', {}, el('b', { textContent: b.label || shortAddr(b.address) }), el('span', { className: 'subl', textContent: b.label ? shortAddr(b.address) : '' })),
    el('td', { className: 'num' }, el('button', { type: 'button', className: 'sm', textContent: 'Review', onclick: () => { wal.q = b.address; walletStart(false); } }), ' ', ...(GUEST ? [] : [el('a', { className: 'sm', href: '#monitor', textContent: 'Monitor', onclick: async () => { try { await radarCall('/api/radar/monitor/wallets', { address: b.address, label: b.label }); } catch (e) { /* shown on the Settings page */ } } }), ' ']),
      el('button', { type: 'button', className: 'sm danger', textContent: 'Remove', onclick: async () => { wal.saved = (await radarCall('/api/radar/bookmarks/remove', { address: b.address })).wallets; renderWallets(); } }))]);
  out.push(labPanel('Saved wallets', 'Wallets you want to come back to.', [], savedRows.length ? table(['Wallet', ''], savedRows) : el('p', { className: 'small', textContent: 'None yet. Review a wallet and press Save wallet.' })));
  if (wal.status === 'running') { const box = rrProgressBox('Reviewing wallet', async () => { try { await radarCall('/api/radar/wallet/cancel', { address: wal.q, days: wal.days, allTransfers: wal.all }); } catch (e) { /* ignore */ } }); rrProgress(box, wal.progress || {}, wal.startedAt, wal.samples); box.cancel.disabled = (wal.progress || {}).stage === 'Cancelling'; out.push(box.node, el('p', { className: 'small', textContent: 'Big wallets can take a few minutes. Transactions are cached, so the next review of this wallet is much faster.' })); }
  else if (!wal.data) out.push(el('p', { className: 'small', textContent: "Paste a wallet address. It downloads the wallet's on-chain history, values every trade in USD at the time it happened, and works out profit and loss per token, how it trades, and where the money goes. It needs a fast RPC: set one on the Settings page." }));
  if (wal.data) out.push(...walletReport(wal.data));
  const focus = document.activeElement && root.contains(document.activeElement) && document.activeElement.tagName === 'INPUT' && document.activeElement.type === 'text';
  if (!focus || wal.status !== 'running') root.replaceChildren(...out);
}

function walletReport(r) {
  const s = r.summary, T = s.totals, st = s.style, out = [], held = h => h < 1 ? Math.round(h * 60) + 'm' : h < 48 ? h.toFixed(1) + 'h' : (h / 24).toFixed(1) + 'd', name = t => t.symbol || shortAddr(t.mint);
  const tokLink = t => el('a', { href: '#coins/' + t.mint, textContent: name(t), title: 'Review ' + t.mint + ' on the Coins Review page' });
  const box = (l, v, sub) => el('div', { className: 'rr-metric' }, el('span', { className: 'l', textContent: l }), el('span', { className: 'v' }, v), el('span', { className: 's', textContent: sub || '' }));
  const parts = [`Over the last ${r.days} days this wallet bought ${rrMoney(T.bought)} of ${s.counts.tokens} meme coins and is ${T.total >= 0 ? 'up' : 'down'} ${rrMoney(Math.abs(T.total))}.`];
  if (s.closedPnl < 0 && T.total > 0) parts.push(`Its finished trades lost ${rrMoney(Math.abs(s.closedPnl))}, so the profit comes from a few big wins and open positions.`); else if (s.closedPnl > 0) parts.push(`Finished trades made ${rrMoney(s.closedPnl)}.`);
  const crit = s.findings.find(f => f.sev === 'crit'); if (crit) parts.push(`Biggest problem: ${crit.title.toLowerCase()} (${crit.metric}).`);
  const saved = wal.saved.some(b => b.address === r.wallet);
  const saveBtn = el('button', { type: 'button', className: 'sm', textContent: saved ? 'Saved' : 'Save wallet', disabled: saved, onclick: async () => { wal.saved = (await radarCall('/api/radar/bookmarks', { address: r.wallet, label: '' })).wallets; renderWallets(); } });
  out.push(el('section', { className: 'card lab' }, el('div', { className: 'rr-hero' }, el('div', {},
    el('span', { className: 'label small', textContent: `Last ${r.days} days · since ${new Date(r.since * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })} · reviewed ${new Date(r.generatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` }),
    el('div', {}, el('code', { textContent: r.wallet })), el('div', { className: 'rr-links' }, rrA('https://solscan.io/account/' + r.wallet, 'Solscan'), rrA('https://gmgn.ai/sol/address/' + r.wallet, 'GMGN'), rrA('https://birdeye.so/profile/' + r.wallet + '?chain=solana', 'Birdeye'),
      el('button', { type: 'button', className: 'sm', textContent: 'Refresh', onclick: () => walletStart(true) }), saveBtn),
    el('div', {}, el('span', { className: 'rr-big ' + (T.total >= 0 ? 'pos' : 'neg'), textContent: rrMoney(T.total, true) }), el('span', { className: 'rr-pill sev-' + s.verdict.sev, textContent: s.verdict.label })), el('p', { textContent: parts.join(' ') })),
    el('div', { className: 'rr-verdicts' }, box('Realized', rrPnl(T.realized), 'from sold tokens'), box('Unrealized', rrPnl(T.unrealized), s.counts.open ? `${s.counts.open} open position${s.counts.open > 1 ? 's' : ''} worth ${rrMoney(T.heldValue)}` : 'no open positions'),
      box('Finished trades', rrPnl(s.closedPnl), `${s.counts.closed} tokens fully exited` + (s.counts.dead ? ` · ${s.counts.dead} dead coins still held (${rrMoney(s.deadHeld.pnl, true)})` : '')), box('Holds now', rrMoney(s.holdings.solUsd + s.holdings.tokensUsd), `${s.holdings.sol.toFixed(2)} SOL + ${rrMoney(s.holdings.tokensUsd)} in tokens`)))));
  // today / yesterday / 7 days
  if (Array.isArray(r.recentActivity)) {
    const mid = new Date(r.generatedAt); mid.setHours(0, 0, 0, 0); const today = mid.getTime() / 1000, yest = new Date(mid.getFullYear(), mid.getMonth(), mid.getDate() - 1).getTime() / 1000, week = new Date(mid.getFullYear(), mid.getMonth(), mid.getDate() - 6).getTime() / 1000, end = r.generatedAt / 1000 + 60;
    const fill = (label, from, to) => { const xs = r.recentActivity.filter(x => x.t >= from && x.t < to), sales = xs.filter(x => x.side === 'sell'), known = sales.filter(x => x.pnl != null), cash = xs.filter(x => x.side === 'cash'), buys = xs.filter(x => x.side === 'buy');
      return box(label, known.length || cash.length ? rrPnl([...known, ...cash].reduce((a, x) => a + x.pnl, 0)) : '—', xs.length ? `${sales.length} sale${sales.length === 1 ? '' : 's'}${known.length ? ` (${known.filter(x => x.pnl > 0).length} won)` : ''} · ${buys.length} buy${buys.length === 1 ? '' : 's'} for ${rrMoney(buys.reduce((a, x) => a + x.usd, 0))}` : 'no trades'); };
    out.push(el('div', { className: 'rr-strip' }, fill('Today', today, end), fill('Yesterday', yest, today), fill('Last 7 days', week, end)), el('p', { className: 'small', textContent: "Profit from sales in each period, against each coin's average cost, by this browser's clock. Open positions aren't counted, so these won't add up to the Realized total." + (r.since > week ? ' The review window is shorter than 7 days.' : '') }));
  }
  out.push(walletChart(r, T));
  out.push(el('div', { className: 'rr-strip' }, rrMetric('Volume bought', rrMoney(T.bought), `${rrMoney(T.proceeds)} sold`), rrMetric('Tokens traded', s.counts.tokens.toLocaleString(), `${s.counts.buys} buys · ${s.counts.sells} sells${s.counts.swaps ? ` · ${s.counts.swaps} swaps` : ''}`),
    rrMetric('Win rate', s.winRate == null ? '—' : s.winRate.toFixed(0) + '%', 'of finished tokens'), rrMetric('Profit factor', s.profitFactor == null ? '—' : s.profitFactor.toFixed(2), 'gains ÷ losses'), rrMetric('Typical buy', rrMoney(st.medianBuy), `90% under ${rrMoney(st.p90Buy)}`), rrMetric('Typical hold', held(s.medianHoldMinutes / 60), `${s.activity.daysActive} active days`)));
  // recent trades
  const trades = (r.recentTrades || []).filter(x => !wal.side || x.side === wal.side), side = el('select', {}, ...[['', 'All trades'], ['buy', 'Buys'], ['sell', 'Sells']].map(([v, l]) => el('option', { value: v, textContent: l, selected: wal.side === v }))); side.onchange = () => { wal.side = side.value; wal.rt = 25; renderWallets(); };
  const sells = trades.filter(x => x.side === 'sell' && x.pnl != null);
  out.push(labPanel('Recent trades', `${trades.length} trade${trades.length === 1 ? '' : 's'} · realized on sales ${rrMoney(sells.reduce((a, x) => a + x.pnl, 0), true)} · winning sales ${sells.filter(x => x.pnl > 0).length}/${sells.length}`, [el('label', { className: 'chk' }, 'Show ', side)],
    table(['When', 'Token', 'Side', '#Value', '#Cost', '#P&L', '#Return', 'Tx'], trades.slice(0, wal.rt).map(x => [when(x.t * 1000),
      el('td', {}, tokLink({ mint: x.mint, symbol: x.symbol }), x.via ? ` ${x.side === 'sell' ? 'into' : 'from'} ${x.viaSymbol || shortAddr(x.via)}` : ''), el('td', {}, rtag(x.side, x.side === 'buy' ? 'p' : 'n'), x.burned ? ' burned' : x.dumped ? ' dumped' : ''),
      x.usd == null ? 'swap' : rrMoney(x.usd), x.side === 'sell' && x.pnl != null ? rrMoney(x.cost) : '—', x.side !== 'sell' ? '—' : x.pnl == null ? 'bought before window' : el('td', {}, rrPnl(x.pnl)), x.side === 'sell' ? (x.pnlPct == null ? '—' : (x.pnlPct > 0 ? '+' : '') + x.pnlPct.toFixed(0) + '%') : '—',
      el('td', {}, /^[1-9A-HJ-NP-Za-km-z]{64,90}$/.test(x.sig || '') ? rrA('https://solscan.io/tx/' + x.sig, 'view') : '')])),
    ...(trades.length > wal.rt ? [el('button', { type: 'button', className: 'sm', textContent: `Show more (${trades.length - wal.rt} left)`, onclick: () => { wal.rt += 50; renderWallets(); } })] : []),
    el('p', { className: 'small', textContent: (r.tradeCount > (r.recentTrades || []).length ? `Showing the newest ${(r.recentTrades || []).length} of ${r.tradeCount.toLocaleString()} trades in this review. ` : '') + "A sale's P&L is what it brought in minus the average cost of the tokens sold. Values are in USD at the time of the trade." })));
  // findings, months
  const maxM = Math.max(1, ...s.monthly.map(m => Math.abs(m.pnl)));
  out.push(el('div', { className: 'rr-cols' }, labPanel('Findings', 'Worst first', [], el('ul', { className: 'rr-list' }, ...(s.findings.length ? s.findings.map(f => el('li', { className: 'sev-' + f.sev }, el('span', { className: 't', textContent: f.title }), el('span', { className: 'm', textContent: f.metric }), el('span', { className: 'd', textContent: f.detail }))) : [el('li', { textContent: 'Nothing stands out.' })]))),
    labPanel('P&L by month', 'By month of last trade', [], ...(s.monthly.length ? s.monthly.map(m => { const w = (Math.abs(m.pnl) / maxM * 100).toFixed(1); return el('div', { className: 'rr-month' }, el('span', { textContent: new Date(m.month + '-01T00:00:00Z').toLocaleDateString([], { month: 'short', year: '2-digit', timeZone: 'UTC' }) }), el('span', { className: 'l' }, ...(m.pnl < 0 ? [el('i', { style: `width:${w}%` })] : [])), el('span', { className: 'r' }, ...(m.pnl > 0 ? [el('i', { style: `width:${w}%` })] : [])), el('span', { className: m.pnl >= 0 ? 'pos' : 'neg', textContent: rrMoney(m.pnl, true) })); }) : [el('p', { className: 'small', textContent: 'No trades in this window.' })]))));
  const row = t => [el('td', {}, tokLink(t)), el('td', {}, rrPnl(t.total)), t.roi == null ? '—' : (t.roi > 0 ? '+' : '') + t.roi.toFixed(0) + '%', `${t.buys}/${t.sells + t.disposals}${t.swapsIn ? ` +${t.swapsIn} swap` : ''}`, held(t.holdHours) + (t.status === 'dead' ? ' still held' : '')];
  out.push(el('div', { className: 'rr-cols' }, labPanel('Best trades', '', [], s.best.length ? table(['Token', 'P&L', 'Return', 'Trades', 'Held'], s.best.map(row)) : el('p', { className: 'small', textContent: 'No winning tokens.' })), labPanel('Worst trades', '', [], s.worst.length ? table(['Token', 'P&L', 'Return', 'Trades', 'Held'], s.worst.map(row)) : el('p', { className: 'small', textContent: 'No losing tokens.' }))));
  const maxH = Math.max(1, ...st.hours);
  const fact = (v, l) => el('div', {}, el('b', { textContent: String(v) }), el('span', { textContent: l }));
  out.push(el('div', { className: 'rr-cols' }, labPanel('How it trades', '', [], el('div', { className: 'rr-facts' }, fact(rrMoney(st.medianBuy), 'median buy'), fact(rrMoney(st.maxBuy), 'largest buy'), fact(st.selfSigned == null ? '—' : st.selfSigned.toFixed(0) + '%', 'buys it signed itself'),
      fact(st.entryLagMinutes == null ? '—' : held(st.entryLagMinutes / 60), 'first buy after launch (median)'), fact(st.entryUnder5 == null ? '—' : st.entryUnder5.toFixed(0) + '%', 'first buys within 5 min of launch'), fact(st.buysPerDay ?? '—', 'buys per active day'),
      fact(`${st.paidSol} / ${st.paidStable}`, 'buys paid in SOL / stablecoins'), fact(st.roundShare == null ? '—' : st.roundShare.toFixed(0) + '%', 'round-number buy sizes'), fact(`${s.deadTokens}/${s.counts.tokens}`, 'tokens now dead')),
      el('span', { className: 'label small', textContent: 'Buys by hour (UTC)' }), el('div', { className: 'rr-hours' }, ...st.hours.map((n, h) => el('i', { style: `height:${(n / maxH * 100).toFixed(1)}%`, title: `${String(h).padStart(2, '0')}:00 UTC · ${n} buys` })))),
    labPanel('Holding period', 'Finished trades', [], table(['Held for', 'Tokens', 'P&L'], s.holds.map(h => [h.label, String(h.tokens), el('td', {}, rrPnl(h.pnl))])),
      table(['Habit', 'Tokens', 'P&L'], [['Averaging down (3+ buys, 1 sell)', s.averagingDown.tokens, s.averagingDown.pnl], ['One buy only', s.singleBuy.tokens, s.singleBuy.pnl], ...(s.deadHeld.tokens ? [['Dead coins still held', s.deadHeld.tokens, s.deadHeld.pnl]] : [])].map(([l, n, p]) => [l, String(n), el('td', {}, rrPnl(p))])))));
  const flow = (title, f) => el('div', {}, el('span', { className: 'label small', textContent: title }), el('div', {}, el('b', { textContent: rrMoney(Math.abs(f.usd)) }), ` ${f.count} transfers · ${Math.abs(f.sol).toFixed(1)} SOL · ${rrMoney(Math.abs(f.stable))} stablecoins`),
    ...f.top.map(c => el('div', { className: 'small' }, c.address === 'unknown' ? 'Unlabeled' : rrA('https://solscan.io/account/' + c.address, shortAddr(c.address), c.address), ` ${c.count}× · ${rrMoney(Math.abs(c.usd))}`)));
  const rc = s.reconcile;
  out.push(labPanel('Money flows', 'Transfers in and out, not trades', [], el('div', { className: 'rr-cols' }, flow('Money in', s.flows.deposit), flow('Money out', s.flows.withdraw)),
    el('p', { className: 'small', textContent: (rc ? `Check: SOL balance went from ${rc.startSol.toFixed(3)} to ${rc.endSol.toFixed(3)}; the transactions account for ${rc.netSolFlows >= 0 ? '+' : ''}${rc.netSolFlows.toFixed(3)} SOL (gap ${rc.gap.toFixed(3)})${rc.partial ? ', which is expected because incoming transfers were skipped' : ''}. ` : '') + (r.txCount.skippedTransfers ? `Money in leaves out ${r.txCount.skippedTransfers.toLocaleString()} skipped incoming transfers. ` : '') + (s.converts.count ? `SOL↔stablecoin conversions: ${s.converts.count}, net ${rrMoney(s.converts.usd, true)}.` : '') })));
  // every token
  const q = wal.filter.trim().toLowerCase();
  let list = r.tokens.filter(t => !q || (t.symbol || '').toLowerCase().includes(q) || t.mint.toLowerCase().includes(q));
  const val = t => wal.sort === 'trades' ? t.buys + t.sells : wal.sort === 'symbol' ? (t.symbol || t.mint).toLowerCase() : wal.sort === 'proceeds' ? t.proceeds + (t.swapProceeds || 0) : (t[wal.sort] ?? -Infinity);
  list.sort((a, b) => { const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * wal.dir; });
  const limit = wal.showAll ? list.length : 50, sortBtn = (k, label) => el('button', { type: 'button', className: 'sm', textContent: label + (wal.sort === k ? (wal.dir < 0 ? ' ↓' : ' ↑') : ''), onclick: () => { if (wal.sort === k) wal.dir = -wal.dir; else { wal.sort = k; wal.dir = k === 'symbol' ? 1 : -1; } renderWallets(); } });
  const filt = el('input', { type: 'search', placeholder: 'Symbol or mint', value: wal.filter, 'aria-label': 'Filter tokens' }); filt.oninput = () => { wal.filter = filt.value; clearTimeout(filt.t); filt.t = setTimeout(renderWallets, 250); };
  out.push(labPanel('Every token', '', [el('label', { className: 'chk' }, 'Filter ', filt)], el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px' }, el('span', { className: 'small', textContent: 'Sort:' }), sortBtn('symbol', 'Token'), sortBtn('total', 'P&L'), sortBtn('roi', 'Return'), sortBtn('invested', 'Invested'), sortBtn('proceeds', 'Sold for'), sortBtn('trades', 'Buys / sells'), sortBtn('holdHours', 'Held'), sortBtn('heldValue', 'Still held')),
    table(['Token', '#P&L', '#Return', '#Invested', '#Sold for', '#Buys / sells', '#Held', '#Still held'], list.slice(0, limit).map(t => [el('td', {}, tokLink(t)), el('td', {}, rrPnl(t.total)), t.roi == null ? '—' : (t.roi > 0 ? '+' : '') + t.roi.toFixed(0) + '%', rrMoney(t.invested), rrMoney(t.proceeds + (t.swapProceeds || 0)),
      `${t.buys} / ${t.sells + t.disposals}${t.swapsIn || t.swapsOut ? ` (+${t.swapsIn}/${t.swapsOut} swaps)` : ''}`, held(t.holdHours), t.status === 'open' ? rrMoney(t.heldValue) : t.status === 'dead' ? 'dead' : t.status === 'unpriced' ? 'no price' : '—'])),
    ...(list.length > limit ? [el('button', { type: 'button', className: 'sm', textContent: `Show all ${list.length}`, onclick: () => { wal.showAll = true; renderWallets(); } })] : [])));
  const a = s.adjustments, adj = [];
  if (a.freeSold >= 1) adj.push(`${rrMoney(a.freeSold)} from selling airdropped tokens counts as profit`); if (a.unknownProceeds >= 1) adj.push(`${rrMoney(a.unknownProceeds)} from selling tokens bought before the window is left out`);
  if (a.transferredOutBasis >= 1) adj.push(`tokens costing ${rrMoney(a.transferredOutBasis)} were sent to other wallets and are left out`); if (a.untracked >= 1) adj.push(`${rrMoney(a.untracked)} of tokens left the wallet in ways the history doesn't show and count as losses`);
  out.push(el('p', { className: 'small', textContent: (adj.length ? 'Adjustments: ' + adj.join('; ') + '. ' : '') + `Based on ${r.txCount.downloaded.toLocaleString()} transactions. Trades are valued at the hourly SOL price when they happened. Cost basis uses running average cost. Not advice.` }));
  return out;
}

// P&L over time: the running total of realized profit after each sale (r.pnlSeries: [t, usd]), drawn as a step line.
function walletChart(r, T) {
  const NS = 'http://www.w3.org/2000/svg', mk = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text != null) e.textContent = text; return e; };
  const ranges = [[7, '7D'], [30, '30D'], [0, 'All']].map(([d, l]) => el('button', { type: 'button', className: 'sm', textContent: l, 'aria-pressed': String(wal.range === d), onclick: () => { wal.range = d; rrStore('rr:pcrange', d); renderWallets(); } }));
  const body = [], all = (r.pnlSeries || []).filter(p => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]));
  if (!all.length) body.push(el('p', { className: 'small', textContent: 'No sales with a known profit in this window yet.' }));
  else {
    const t1 = Math.max(r.generatedAt / 1000, all[all.length - 1][0] + 1), t0 = wal.range ? Math.max(r.since, t1 - wal.range * 86400) : r.since, before = all.filter(p => p[0] < t0), series = all.filter(p => p[0] >= t0), v0 = before.length ? before[before.length - 1][1] : 0;
    const pts = [[t0, v0], ...series, [t1, (series[series.length - 1] || [0, v0])[1]]], last = pts[pts.length - 1][1];
    let peak = pts[0], dd = 0, run = -Infinity; for (const p of pts) { if (p[1] > peak[1]) peak = p; run = Math.max(run, p[1]); dd = Math.max(dd, run - p[1]); }
    const W = 760, H = 240, mL = 58, mR = 20, mT = 12, mB = 24; let lo = Math.min(0, ...pts.map(p => p[1])), hi = Math.max(0, ...pts.map(p => p[1])); if (hi - lo < 1) { hi += .5; lo -= .5; }
    const X = t => mL + (t - t0) / (t1 - t0) * (W - mL - mR), Y = v => mT + (1 - (v - lo) / (hi - lo)) * (H - mT - mB), svg = mk('svg', { viewBox: `0 0 ${W} ${H}`, class: 'rr-pc', role: 'img', 'aria-label': 'Realized profit over time' });
    for (let i = 0; i <= 4; i++) { const v = lo + (hi - lo) * i / 4; svg.append(mk('line', { x1: mL, x2: W - mR, y1: Y(v), y2: Y(v), class: 'grid' }), mk('text', { x: mL - 6, y: Y(v) + 4, 'text-anchor': 'end' }, rrMoney(v, true))); }
    svg.append(mk('line', { x1: mL, x2: W - mR, y1: Y(0), y2: Y(0), class: 'zero' }));
    for (let i = 0; i <= 4; i++) { const t = t0 + (t1 - t0) * i / 4; svg.append(mk('text', { x: X(t), y: H - 6, 'text-anchor': 'middle' }, new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' }))); }
    let d = `M${X(pts[0][0]).toFixed(1)},${Y(pts[0][1]).toFixed(1)}`; for (let i = 1; i < pts.length; i++) d += ` H${X(pts[i][0]).toFixed(1)} V${Y(pts[i][1]).toFixed(1)}`;
    svg.append(mk('path', { d, class: 'ln ' + (last >= 0 ? 'up' : 'down') }));
    body.push(el('div', { className: 'rr-sum' }, el('span', {}, 'Realized ', rrPnl(last - v0)), el('span', {}, 'Peak ', rrPnl(peak[1])), el('span', {}, 'Largest drawdown ', dd > 0 ? rrPnl(-dd) : '—'), el('span', { textContent: `${(r.pnlSales ?? series.length).toLocaleString()} sales with a known profit` })), svg,
      el('p', { className: 'small', textContent: `Each step is a sale's profit against the coin's average cost. The wallet's total P&L, holdings included, is ${rrMoney(T.total, true)}.` }));
  }
  return labPanel('P&L over time', '', [el('span', { style: 'display:flex;gap:4px' }, ...ranges)], ...body);
}
