// The Coins Review, Wallets Review and Monitor pages (ported from Rug Radar). Loaded by index.html before its own script; everything here runs
// after that script has defined el(), labPanel(), table(), usd(), when() and the rest, so it only uses them inside functions.
'use strict';

const radarCall = async (url, body) => {
  const res = await fetch(url, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'HTTP ' + res.status);
  return d;
};
const shortAddr = a => a ? a.slice(0, 4) + '…' + a.slice(-4) : '';
const radarAgo = ms => { if (!ms) return 'never'; const s = Math.max(0, Math.round((Date.now() - ms) / 1000)); return s < 60 ? s + ' s ago' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
const amountText = n => { const a = Math.abs(n); return a >= 1e9 ? (a / 1e9).toFixed(2) + 'B' : a >= 1e6 ? (a / 1e6).toFixed(2) + 'M' : a >= 1e3 ? (a / 1e3).toFixed(1) + 'K' : a >= 1 ? a.toFixed(2) : a.toPrecision(3); };
const solText = n => Math.abs(n) >= 1 ? Math.abs(n).toFixed(3) : Math.abs(n).toFixed(4);
const coinName = l => '$' + (l.symbol || l.mint.slice(0, 4) + '…' + l.mint.slice(-4));
const radarLink = (href, text) => el('a', { href, target: '_blank', rel: 'noopener noreferrer', textContent: text });
const httpOnly = u => /^https?:\/\//.test(u || '') ? u : null;
const rtag = (txt, cls, title) => el('span', { className: 'tag ' + (cls || ''), textContent: txt, title: title || '' });
const radarMsg = (node, text, bad) => { node.textContent = text || ''; node.className = 'small ' + (bad ? 'n' : 'p'); node.style.margin = '6px 0 0'; };

// ---- Monitor ----------------------------------------------------------------------------------------------------------
const mon = { d: null, wallet: '', buys: false, editBot: false, busy: false, rpcOpen: false };

async function loadMonitor() {
  if (mon.busy) return;
  mon.busy = true;
  try {
    mon.d = await radarCall('/api/radar/monitor' + (mon.wallet || mon.buys ? '?' + new URLSearchParams({ ...(mon.wallet ? { wallet: mon.wallet } : {}), ...(mon.buys ? { kind: 'buy' } : {}) }) : ''));
    renderMonitor();
  } catch (e) { $('monitor').replaceChildren(el('p', { className: 'small n', textContent: e.message })); }
  mon.busy = false;
}
const typing = () => { const a = document.activeElement; return !!a && $('monitor').contains(a) && ['INPUT', 'SELECT'].includes(a.tagName) && a.type !== 'checkbox'; };
setInterval(() => { if ($('tab-monitor').hidden || document.hidden || mon.editBot || typing()) return; loadMonitor(); }, 4000);

function evTrade(ev) {
  const l = ev.legs[0];
  switch (ev.kind) {
    case 'buy': return `Bought ${amountText(l.amount)} ${coinName(l)}`;
    case 'sell': return ev.dust ? `Sold ${amountText(l.amount)} ${coinName(l)} for dust` : `Sold ${amountText(l.amount)} ${coinName(l)}`;
    case 'swap': { if (!ev.legs.length) return ev.sol < 0 ? `Swapped ${solText(ev.sol)} SOL for USDC/USDT` : `Swapped USDC/USDT for ${solText(ev.sol)} SOL`;
      return 'Swapped ' + ev.legs.filter(x => x.amount < 0).map(x => amountText(x.amount) + ' ' + coinName(x)).join(' + ') + ' for ' + ev.legs.filter(x => x.amount > 0).map(x => amountText(x.amount) + ' ' + coinName(x)).join(' + '); }
    default: return ev.kind;
  }
}

function renderMonitor() {
  const d = mon.d, root = $('monitor'), out = [];
  const slow = d.slowedBy ? ` · checking every ${d.pollSeconds} s (${d.slowedBy})` : '';
  out.push(el('p', { className: 'small', style: 'margin:0 0 10px', textContent: `${d.wallets.length} of ${d.maxWallets} wallets monitored · checked every ${d.basePollSeconds} s${slow}.` + (d.updated && Date.now() - d.updated > 60000 ? ' The monitor service has not updated for a while: it may have stopped.' : '') }));

  // the RPC keys live on the Settings page; the monitor page only says where
  out.push(el('p', { className: 'small', style: 'margin:0 0 10px' }, d.rpc.custom ? `Reading the chain through ${d.rpc.keys} RPC key${d.rpc.keys === 1 ? '' : 's'}. ` : 'Using the free public RPC, which is slow. ', el('a', { href: '#settings', textContent: 'Manage the RPC keys in Settings' }), '.'));

  // the Telegram bot
  const tg = d.telegram, botMsg = el('p', {});
  if (tg.configured && !mon.editBot) {
    const test = el('button', { type: 'button', className: 'sm', textContent: 'Send test message' }), change = el('button', { type: 'button', className: 'sm', textContent: 'Change' });
    test.onclick = async () => { try { await radarCall('/api/radar/monitor/telegram/test', {}); radarMsg(botMsg, 'Test message sent.'); } catch (e) { radarMsg(botMsg, e.message, true); } };
    change.onclick = () => { mon.editBot = true; renderMonitor(); };
    out.push(labPanel('Telegram bot', 'The wallet monitor uses its own bot, separate from the trade alerts bot.', [],
      el('p', { className: 'small', style: 'margin:0' }, `Sending with bot ${tg.token_hint} to chat ${tg.chat_id} `, test, ' ', change), botMsg));
  } else {
    const token = el('input', { type: 'password', placeholder: '123456789:AAH…', spellcheck: false, autocomplete: 'off' }), chat = el('input', { type: 'text', placeholder: '123456789, -100… or @channel', spellcheck: false, value: tg.chat_id || '' });
    const chats = el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;margin-top:6px' });
    const detect = el('button', { type: 'button', className: 'sm', textContent: 'Detect chats' }), save = el('button', { type: 'button', className: 'primary', textContent: 'Save' }), test = el('button', { type: 'button', textContent: 'Send test message' });
    detect.onclick = async () => { try { const r = await radarCall('/api/radar/monitor/telegram/chats', { token: token.value }); chats.replaceChildren(...(r.chats.length ? r.chats : []).map(c => { const b = el('button', { type: 'button', className: 'sm', textContent: `${c.name} (${c.id})` }); b.onclick = () => { chat.value = c.id; }; return b; })); radarMsg(botMsg, r.chats.length ? 'Pick your chat.' : 'No chat found. Send your bot any message first, then try again.', !r.chats.length); } catch (e) { radarMsg(botMsg, e.message, true); } };
    save.onclick = async () => { try { await radarCall('/api/radar/monitor/telegram', { token: token.value, chat_id: chat.value }); mon.editBot = false; loadMonitor(); } catch (e) { radarMsg(botMsg, e.message, true); } };
    test.onclick = async () => { try { await radarCall('/api/radar/monitor/telegram/test', { token: token.value, chat_id: chat.value }); radarMsg(botMsg, 'Test message sent.'); } catch (e) { radarMsg(botMsg, e.message, true); } };
    const extra = [];
    if (tg.configured) { const cancel = el('button', { type: 'button', textContent: 'Cancel' }), clear = el('button', { type: 'button', className: 'danger', textContent: 'Remove bot' });
      cancel.onclick = () => { mon.editBot = false; renderMonitor(); }; clear.onclick = async () => { await radarCall('/api/radar/monitor/telegram/clear', {}); mon.editBot = false; loadMonitor(); }; extra.push(cancel, clear); }
    out.push(labPanel('Telegram bot', 'In Telegram, message @BotFather, send /newbot and copy the token. Then send your new bot any message (or add it to a group) and press Detect chats.', [],
      el('div', { className: 'grid2' }, el('div', { className: 'field' }, el('label', { textContent: 'Bot token' }), token), el('div', { className: 'field' }, el('label', { textContent: 'Chat ID' }), chat)),
      el('div', {}, detect), chats, el('div', { style: 'display:flex;gap:8px;margin-top:10px;flex-wrap:wrap' }, save, test, ...extra), botMsg));
  }

  // the monitored wallets
  const addr = el('input', { type: 'text', placeholder: 'Wallet address', spellcheck: false, autocomplete: 'off', 'aria-label': 'Wallet address', style: 'flex:2;min-width:240px' });
  const label = el('input', { type: 'text', placeholder: 'Label (optional)', maxLength: 40, 'aria-label': 'Label', style: 'flex:1;min-width:140px' });
  const addBtn = el('button', { type: 'button', className: 'primary', textContent: 'Monitor' }), addMsg = el('p', {});
  addBtn.onclick = async () => { addBtn.disabled = true; try { await radarCall('/api/radar/monitor/wallets', { address: addr.value.trim(), label: label.value.trim() }); addr.value = label.value = ''; radarMsg(addMsg, 'Added. Activity from now on will be reported.'); loadMonitor(); } catch (e) { radarMsg(addMsg, e.message, true); } addBtn.disabled = false; };
  const act = async (url, body) => { try { await radarCall(url, body); loadMonitor(); } catch (e) { radarMsg(addMsg, e.message, true); } };
  const rows = d.wallets.map(w => {
    const status = w.paused ? rtag('paused', 'w') : w.error ? rtag('error', 'n', w.error) : !w.started ? rtag('starting', 'w') : rtag('watching', 'p', w.pending ? w.pending + ' transactions waiting to be read' : '');
    const buttons = el('td', { className: 'num' },
      el('button', { type: 'button', className: 'sm', textContent: w.paused ? 'Resume' : 'Pause', onclick: () => act('/api/radar/monitor/wallets/update', { address: w.address, paused: !w.paused }) }), ' ',
      el('button', { type: 'button', className: 'sm', textContent: w.muted ? 'Unmute' : 'Mute', title: 'A muted wallet is still recorded, but sends nothing to Telegram', onclick: () => act('/api/radar/monitor/wallets/update', { address: w.address, muted: !w.muted }) }), ' ',
      el('button', { type: 'button', className: 'sm', textContent: 'Rename', onclick: () => { const v = prompt('Label for ' + shortAddr(w.address), w.label || ''); if (v !== null) act('/api/radar/monitor/wallets/update', { address: w.address, label: v }); } }), ' ',
      el('button', { type: 'button', className: 'sm danger', textContent: 'Remove', onclick: () => { if (confirm('Stop monitoring ' + (w.label || shortAddr(w.address)) + '?')) act('/api/radar/monitor/wallets/remove', { address: w.address }); } }));
    return [el('td', {}, el('b', { textContent: w.label || shortAddr(w.address) }), el('span', { className: 'subl' }, radarLink('https://solscan.io/account/' + w.address, shortAddr(w.address)), ' · ', el('a', { href: '#wallets/' + w.address, textContent: 'review' }))),
      el('td', {}, status, ...(w.muted ? [' ', rtag('muted', 'w')] : [])), el('td', { textContent: w.last ? evTrade(w.last) : '–' }, ), { className: 'num', textContent: String(w.events) }, { className: 'num', textContent: String(w.sent) }, radarAgo(w.lastCheck), buttons];
  });
  const optTg = el('input', { type: 'checkbox', checked: d.settings.telegram }), optTr = el('input', { type: 'checkbox', checked: d.settings.transfers }), optMin = el('input', { type: 'text', inputMode: 'decimal', value: String(d.settings.min_sol || 0), style: 'width:70px' });
  const optSave = el('button', { type: 'button', className: 'sm', textContent: 'Save' }), optMsg = el('span', { className: 'small' });
  const saveOpts = async () => { try { await radarCall('/api/radar/monitor/settings', { telegram: optTg.checked, transfers: optTr.checked, min_sol: parseFloat(optMin.value || '0') }); optMsg.textContent = 'Saved.'; } catch (e) { optMsg.textContent = e.message; } };
  optTg.onchange = optTr.onchange = saveOpts; optSave.onclick = saveOpts;
  out.push(labPanel('Monitored wallets', 'The first check only records where each wallet is; trades after that are reported.', [],
    el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px' }, addr, label, addBtn), addMsg,
    d.wallets.length ? table(['Wallet', 'Status', 'Last trade', '#Trades', '#Sent', 'Checked', ''], rows) : el('p', { className: 'small', textContent: 'No wallets yet. Paste an address above.' }),
    el('div', { style: 'display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-top:10px' }, el('label', { className: 'chk' }, optTg, ' Send trades to Telegram'), el('label', { className: 'chk' }, optTr, ' Also send transfers'),
      el('span', { className: 'small' }, 'Skip trades under ', optMin, ' SOL ', optSave), optMsg),
    el('p', { className: 'small', style: 'margin:8px 0 0', textContent: 'Trades are buys, sells and swaps. A wallet that trades more than 8 times between checks gets a few messages and one summary, so a trading bot cannot flood the chat. Transfers are never shown in the activity list.' })));

  // the activity feed
  const sel = el('select', { 'aria-label': 'Wallet' }, el('option', { value: '', textContent: 'All wallets' }), ...d.wallets.map(w => el('option', { value: w.address, textContent: w.label || shortAddr(w.address), selected: w.address === mon.wallet })));
  const buys = el('input', { type: 'checkbox', checked: mon.buys });
  sel.onchange = () => { mon.wallet = sel.value; loadMonitor(); }; buys.onchange = () => { mon.buys = buys.checked; loadMonitor(); };
  const feedRows = d.feed.map(ev => {
    const l = ev.legs.find(x => x.amount > 0) || ev.legs[0], m = ev.market, value = ev.usd != null ? usd(ev.usd) : '–';
    const links = el('td', {}, radarLink('https://solscan.io/tx/' + ev.sig, 'Tx'));
    if (l) { const u = httpOnly(m && m.url); links.append(' · ', radarLink(u || 'https://www.geckoterminal.com/solana/tokens/' + l.mint, u && /dexscreener/.test(u) ? 'DexScreener' : 'GeckoTerminal'));
      if (ev.pumpfun || (m && m.pumpfun) || l.mint.endsWith('pump')) links.append(' · ', radarLink('https://pump.fun/coin/' + l.mint, 'pump.fun'));
      links.append(' · ', el('a', { href: '#coins/' + l.mint, textContent: 'Check' })); }
    return [subCell(when(ev.t), radarAgo(ev.t)), el('td', { textContent: ev.label || shortAddr(ev.wallet) }), el('td', {}, rtag(ev.kind, ev.kind === 'buy' ? 'p' : ev.kind === 'sell' ? 'n' : 'w')),
      el('td', { textContent: evTrade(ev) }), { className: 'num', textContent: value }, { className: 'num', textContent: m && m.marketCap ? usd(m.marketCap) + (m.late ? ' (later)' : '') : '–' }, links];
  });
  out.push(labPanel('Activity', 'The newest 100 trades of the monitored wallets.', [el('label', { className: 'chk' }, 'Wallet ', sel), el('label', { className: 'chk' }, buys, ' Buys only')],
    feedRows.length ? table(['Time', 'Wallet', 'Type', 'Trade', '#Value', '#Market cap', 'Links'], feedRows) : el('p', { className: 'small', textContent: 'No trades yet.' })));
  // keep what the user is typing: the page refreshes every few seconds
  if (typing()) return;
  root.replaceChildren(...out);
}

// ---- Solana RPC keys: a panel of the Settings page (like Rug Radar's Settings: add a key, then the pool in priority order) ---------------
function rpcKeysPanel() {
  const box = el('div', {}), msg = el('p', {});
  const holder = labPanel('Solana RPC keys', 'Loading…', [], box);
  const act = async (url, body) => { try { draw(await radarCall(url, body)); } catch (e) { radarMsg(msg, e.message, true); } };
  function draw(v) {
    const keys = v.keys || [];
    const input = el('input', { type: 'password', placeholder: 'Alchemy API key or https://… address', spellcheck: false, autocomplete: 'off', 'aria-label': 'Alchemy API key or RPC address', style: 'flex:2;min-width:220px' });
    const label = el('input', { type: 'text', placeholder: 'Label (optional)', maxLength: 40, 'aria-label': 'Label', style: 'flex:1;min-width:120px' });
    const add = el('button', { type: 'button', className: 'primary', textContent: 'Test and add' });
    add.onclick = async () => { add.disabled = true; radarMsg(msg, 'Testing…'); try { const r = await radarCall('/api/radar/rpc/add', { key: input.value.trim(), label: label.value.trim() }); draw(r); radarMsg(msg, `Added. It answered in ${r.tested.ms} ms.`); } catch (e) { radarMsg(msg, e.message, true); add.disabled = false; } };
    const mode = el('select', { 'aria-label': 'Rotation mode' }, el('option', { value: 'failover', textContent: 'Failover: use the first key, switch when it is limited', selected: v.mode === 'failover' }), el('option', { value: 'roundrobin', textContent: 'Round-robin: spread the load over all keys', selected: v.mode === 'roundrobin' }));
    mode.onchange = () => act('/api/radar/rpc/mode', { mode: mode.value });
    const rows = keys.map((k, i) => [i + 1, el('td', {}, el('b', { textContent: k.label || 'Key' })), el('td', { className: 'mono', textContent: k.key }), el('td', {}, rtag(k.enabled ? 'on' : 'off', k.enabled ? 'p' : 'w')),
      el('td', { className: 'num' }, el('button', { type: 'button', className: 'sm', textContent: k.enabled ? 'Turn off' : 'Turn on', onclick: () => act('/api/radar/rpc/update', { id: k.id, enabled: !k.enabled }) }), ' ',
        el('button', { type: 'button', className: 'sm', textContent: 'Test', onclick: async () => { radarMsg(msg, 'Testing…'); try { const r = await radarCall('/api/radar/rpc/test', { id: k.id }); radarMsg(msg, `${k.label || 'Key'} answered in ${r.ms} ms.`); } catch (e) { radarMsg(msg, e.message, true); } } }), ' ',
        el('button', { type: 'button', className: 'sm', textContent: 'Rename', onclick: () => { const nv = prompt('Label for this key', k.label || ''); if (nv !== null) act('/api/radar/rpc/update', { id: k.id, label: nv }); } }), ' ',
        el('button', { type: 'button', className: 'sm danger', textContent: 'Remove', onclick: () => { if (confirm('Remove ' + (k.label || 'this key') + '?')) act('/api/radar/rpc/remove', { id: k.id }); } }))]);
    holder.querySelector('.lab-head .small').textContent = v.custom ? `${keys.filter(k => k.enabled).length} of ${keys.length} keys in use` : 'none yet: using the free public RPC';
    box.replaceChildren(
      el('p', { className: 'small', style: 'margin:0 0 8px', textContent: "The Coins Review, Wallets Review and Monitor pages download thousands of transactions, which needs a fast RPC. Add one or more Alchemy API keys (or any provider's https address). When a key hits its rate limit it rests and the next one takes over. These keys are separate from the RPC the trader uses (above)." }),
      el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center' }, input, label, add), msg,
      keys.length ? table(['No.', 'Label', 'Key', 'State', ''], rows) : el('p', { className: 'small', textContent: 'No keys yet, so wallet reviews use the public Solana RPC, which can take hours for a busy wallet.' }),
      ...(keys.length ? [el('div', { style: 'margin-top:8px' }, mode)] : []));
  }
  radarCall('/api/radar/rpc').then(draw).catch(e => box.replaceChildren(el('p', { className: 'small n', textContent: e.message })));
  holder.id = 'rpckeys'; return holder;
}
