import re
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
LOG_PATH = os.path.join(os.path.dirname(__file__), "spy_bot.log")

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>SPY Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:      #080b12;
  --s1:      #0d1119;
  --s2:      #111620;
  --border:  #1a2438;
  --border2: #243050;
  --text:    #e0eaf8;
  --muted:   #4a6280;
  --faint:   #151d2e;
  --accent:  #3b82f6;
  --green:   #22c55e;
  --red:     #ef4444;
  --yellow:  #f59e0b;
  --purple:  #a78bfa;
}

html { font-size: 16px; }
body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; min-height: 100vh; }

/* ─── HEADER ─── */
header {
  background: var(--s1);
  border-bottom: 1px solid var(--border);
  padding: 0 20px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky; top: 0; z-index: 100;
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand-icon {
  width: 34px; height: 34px;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 15px; color: #fff; letter-spacing: -1px;
}
.brand-name { font-size: 15px; font-weight: 700; color: var(--text); }
.brand-sub  { font-size: 11px; color: var(--muted); font-family: 'JetBrains Mono', monospace; margin-top: 1px; }

.status-chip {
  display: flex; align-items: center; gap: 7px;
  padding: 6px 14px; border-radius: 20px;
  font-size: 12px; font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase;
  border: 1px solid transparent;
}
.status-chip .dot { width: 7px; height: 7px; border-radius: 50%; }
.status-chip.running { background:#22c55e10; color:var(--green); border-color:#22c55e30; }
.status-chip.running .dot { background:var(--green); animation:pulse 2s infinite; }
.status-chip.waiting { background:#f59e0b10; color:var(--yellow); border-color:#f59e0b30; }
.status-chip.waiting .dot { background:var(--yellow); }
.status-chip.stopped { background:#ef444410; color:var(--red); border-color:#ef444430; }
.status-chip.stopped .dot { background:var(--red); }
@keyframes pulse { 0%,100%{box-shadow:0 0 0 0 #22c55e55}50%{box-shadow:0 0 0 5px #22c55e00} }

/* ─── CLOCK HERO ─── */
.clock-hero {
  background: var(--s2);
  border-bottom: 1px solid var(--border);
  padding: 28px 20px 24px;
  text-align: center;
}
.clock-time {
  font-family: 'JetBrains Mono', monospace;
  font-size: clamp(48px, 10vw, 88px);
  font-weight: 600;
  color: var(--text);
  letter-spacing: -2px;
  line-height: 1;
}
.clock-label {
  font-size: 13px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 2px;
  margin-top: 8px;
}
.clock-date {
  font-size: 14px; color: var(--muted);
  font-family: 'JetBrains Mono', monospace;
  margin-top: 4px;
}

/* ─── MARKET BANNER ─── */
.mkt-banner {
  display: flex; align-items: center; justify-content: center; gap: 10px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  font-size: 13px; font-weight: 500;
}
.mkt-dot { width: 8px; height: 8px; border-radius: 50%; }
.mkt-banner.open   { background:#22c55e08; color:var(--green); }
.mkt-banner.open .mkt-dot { background:var(--green); animation:pulse 2s infinite; }
.mkt-banner.closed { background:transparent; color:var(--muted); }
.mkt-banner.closed .mkt-dot { background:var(--muted); }
.mkt-banner.pre    { background:#f59e0b08; color:var(--yellow); }
.mkt-banner.pre .mkt-dot { background:var(--yellow); }

/* ─── STAT CARDS ─── */
.stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
  padding: 20px;
}
@media(max-width:1050px) { .stats{ grid-template-columns: repeat(3,1fr); } }
@media(max-width:640px)  { .stats{ grid-template-columns: repeat(2,1fr); gap:10px; padding:14px; } }

.card {
  background: var(--s1);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 18px 18px 16px;
  transition: border-color .2s, transform .15s;
  cursor: default;
}
.card:active { transform: scale(.98); }

.card-label {
  font-size: 11px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 1px;
  margin-bottom: 10px;
}
.card-value {
  font-size: clamp(22px, 4vw, 30px);
  font-weight: 800;
  font-family: 'JetBrains Mono', monospace;
  letter-spacing: -1px;
  line-height: 1;
}
.card-value.long  { color: var(--green); }
.card-value.short { color: var(--red);   }
.card-value.flat  { color: var(--muted); }
.card-value.blue  { color: var(--accent);}
.card-sub {
  font-size: 12px; color: var(--muted);
  margin-top: 6px; line-height: 1.4;
}

.card.pos-long  { border-color:#22c55e35; background:linear-gradient(140deg,#22c55e0a,var(--s1)); }
.card.pos-short { border-color:#ef444435; background:linear-gradient(140deg,#ef44440a,var(--s1)); }

/* ─── PANELS ─── */
.panels {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 0 20px 20px;
}
@media(max-width:760px) { .panels{ grid-template-columns:1fr; } }
@media(max-width:480px) { .panels{ padding:0 14px 14px; gap:10px; } }

.panel {
  background: var(--s1);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  display: flex; flex-direction: column;
}

.panel-head {
  padding: 14px 18px;
  background: var(--s2);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0;
}
.panel-title {
  font-size: 12px; font-weight: 700;
  color: var(--muted); text-transform: uppercase; letter-spacing: 1px;
}
.panel-badge {
  font-size: 11px; color: var(--muted);
  background: var(--faint); border: 1px solid var(--border);
  border-radius: 20px; padding: 2px 10px;
}

.panel-body {
  overflow-y: auto;
  flex: 1;
  max-height: 400px;
}
@media(max-width:760px){ .panel-body{ max-height:300px; } }

.panel-body::-webkit-scrollbar { width: 4px; }
.panel-body::-webkit-scrollbar-thumb { background: var(--border2); border-radius:4px; }

/* ─── TRADE ROWS ─── */
.trade-row {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 14px 18px;
  border-bottom: 1px solid var(--faint);
  transition: background .1s;
}
.trade-row:last-child { border-bottom: none; }
.trade-row:hover { background: var(--s2); }

.trade-bar {
  width: 3px; border-radius: 2px; min-height: 40px; flex-shrink: 0; margin-top: 2px;
}
.trade-bar.long   { background: var(--green); }
.trade-bar.short  { background: var(--red);   }
.trade-bar.warn   { background: var(--yellow);}
.trade-bar.info   { background: var(--accent);}
.trade-bar.system { background: var(--muted); }

.trade-body { flex: 1; min-width: 0; }
.trade-chip {
  display: inline-block;
  font-size: 9px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
  padding: 2px 7px; border-radius: 4px; margin-bottom: 5px;
}
.trade-chip.long   { background:#22c55e18; color:var(--green); }
.trade-chip.short  { background:#ef444418; color:var(--red);   }
.trade-chip.warn   { background:#f59e0b18; color:var(--yellow);}
.trade-chip.info   { background:#3b82f618; color:var(--accent);}
.trade-chip.system { background:#ffffff0c; color:var(--muted); }

.trade-text { font-size: 13px; color: var(--text); line-height: 1.5; }
.trade-ts   { font-size: 11px; color: var(--muted); font-family:'JetBrains Mono',monospace; margin-top:4px; }

/* ─── LOG ROWS ─── */
.log-row {
  display: flex; gap: 10px;
  padding: 5px 18px;
  border-bottom: 1px solid #0c1018;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px; line-height: 1.7;
}
.log-row:hover { background: var(--s2); }
.log-ts  { color: var(--muted); flex-shrink: 0; width: 56px; }
.log-msg.info  { color: #6a8ab0; }
.log-msg.warn  { color: var(--yellow); }
.log-msg.error { color: var(--red); }

/* ─── FOOTER ─── */
.panel-foot {
  padding: 9px 18px;
  border-top: 1px solid var(--border);
  background: var(--s2);
  display: flex; align-items: center; gap: 8px; flex-shrink: 0;
}
.foot-blink {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
  animation: blink 2s ease-in-out infinite;
}
@keyframes blink { 0%,100%{opacity:1}50%{opacity:.2} }
.foot-text { font-size: 11px; color: var(--muted); font-family:'JetBrains Mono',monospace; }

/* ─── EMPTY ─── */
.empty {
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  height: 200px; color: var(--muted); font-size: 13px; text-align: center; gap: 10px;
}
.empty-icon { font-size: 32px; opacity: .3; }
</style>
</head>
<body>

<!-- HEADER -->
<header>
  <div class="brand">
    <div class="brand-icon">S</div>
    <div>
      <div class="brand-name">SPY Trading Bot</div>
      <div class="brand-sub" id="acc">Connecting...</div>
    </div>
  </div>
  <div class="status-chip stopped" id="chip">
    <span class="dot"></span><span id="chip-txt">Offline</span>
  </div>
</header>

<!-- CLOCK HERO -->
<div class="clock-hero">
  <div class="clock-time" id="clock">00:00:00</div>
  <div class="clock-label">Eastern Time</div>
  <div class="clock-date" id="clock-date">--</div>
</div>

<!-- MARKET BANNER -->
<div class="mkt-banner closed" id="mkt-banner">
  <div class="mkt-dot"></div>
  <span id="mkt-txt">Checking market...</span>
</div>

<!-- STAT CARDS -->
<div class="stats">
  <div class="card" id="pos-card">
    <div class="card-label">Position</div>
    <div class="card-value flat" id="pos-val">FLAT</div>
    <div class="card-sub" id="pos-sub">No open position</div>
  </div>
  <div class="card">
    <div class="card-label">Equity (ELV)</div>
    <div class="card-value blue" id="elv-val">--</div>
    <div class="card-sub">Available capital</div>
  </div>
  <div class="card">
    <div class="card-label">Candle Open</div>
    <div class="card-value" id="open-val">--</div>
    <div class="card-sub" id="entries-sub">0 / 4 entries</div>
  </div>
  <div class="card">
    <div class="card-label">Leg Size</div>
    <div class="card-value" id="leg-val">--</div>
    <div class="card-sub">Shares per leg</div>
  </div>
  <div class="card">
    <div class="card-label">Strategy PnL</div>
    <div class="card-value flat" id="pnl-val">--</div>
    <div class="card-sub" id="pnl-sub">realized by bot</div>
  </div>
</div>

<!-- PANELS -->
<div class="panels">
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Trade Events</div>
      <span class="panel-badge" id="tc">0</span>
    </div>
    <div class="panel-body" id="trade-panel">
      <div class="empty"><div class="empty-icon">📊</div>Waiting for market open...</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Live Log</div>
      <span class="panel-badge" id="lc">0 lines</span>
    </div>
    <div class="panel-body" id="log-panel">
      <div class="empty"><div class="empty-icon">📋</div>Loading...</div>
    </div>
    <div class="panel-foot">
      <div class="foot-blink"></div>
      <span class="foot-text" id="foot-txt">Initializing...</span>
    </div>
  </div>
</div>

<script>
// ── Clock
const DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function tickClock() {
  const et = new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'}));
  document.getElementById('clock').textContent =
    String(et.getHours()).padStart(2,'0')+':'+String(et.getMinutes()).padStart(2,'0')+':'+String(et.getSeconds()).padStart(2,'0');
  document.getElementById('clock-date').textContent =
    DAYS[et.getDay()]+', '+MONTHS[et.getMonth()]+' '+et.getDate()+' '+et.getFullYear();
}
setInterval(tickClock,1000); tickClock();

// ── Market status
function updateMarket() {
  const et = new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'}));
  const d=et.getDay(), mins=et.getHours()*60+et.getMinutes();
  const banner=document.getElementById('mkt-banner'), txt=document.getElementById('mkt-txt');
  if(d===0||d===6){ banner.className='mkt-banner closed'; txt.textContent='Market Closed — Weekend'; }
  else if(mins>=570&&mins<960){ banner.className='mkt-banner open'; txt.textContent='NYSE & NASDAQ Open — Regular Session 9:30 AM – 4:00 PM ET'; }
  else if(mins>=240&&mins<570){ banner.className='mkt-banner pre'; txt.textContent='Pre-Market Session (4:00 AM – 9:30 AM ET)'; }
  else{ banner.className='mkt-banner closed'; txt.textContent='Market Closed — After Hours'; }
}
setInterval(updateMarket,30000); updateMarket();

// ── Tag helpers
function tagCls(msg){
  if(/Y LONG filled|Reverse filled.*LONG/i.test(msg)) return 'long';
  if(/Z SHORT filled|Reverse filled.*SHORT/i.test(msg)) return 'short';
  if(/WARNING|Risk exit|noon exit|SL fired|halting|Early close/i.test(msg)) return 'warn';
  if(/Connected|Session complete/i.test(msg)) return 'system';
  return 'info';
}
function tagLabel(c){ return{long:'Long Entry',short:'Short Entry',warn:'Warning',system:'System',info:'Event'}[c]; }

// ── Refresh
let logBottom=true;
async function refresh(){
  try{
    const d=await fetch('/api/state').then(r=>r.json());

    if(d.account) document.getElementById('acc').textContent=d.account+' · Paper';

    // status chip
    const chip=document.getElementById('chip');
    const statusMap={running:'Running',waiting:'Waiting',holiday:'Holiday',ended:'Session Ended',stopped:'Offline'};
    const chipClass={running:'running',waiting:'waiting',holiday:'waiting',ended:'waiting',stopped:'stopped'};
    chip.className='status-chip '+(chipClass[d.status]||'stopped');
    document.getElementById('chip-txt').textContent=statusMap[d.status]||d.status;

    // stats
    document.getElementById('elv-val').textContent=d.elv?'$'+Number(d.elv).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'--';
    document.getElementById('open-val').textContent=d.candle_open?'$'+d.candle_open:'--';
    document.getElementById('entries-sub').textContent=(d.entries||0)+' / 4 entries';
    document.getElementById('leg-val').textContent=d.leg_qty||'--';

    const pnlEl=document.getElementById('pnl-val');
    const pnlSub=document.getElementById('pnl-sub');
    const bp=(d.bot_pnl!==null&&d.bot_pnl!==undefined)?d.bot_pnl:null;
    if(bp!==null){
      const pct=(d.elv&&d.elv>0)?(bp/d.elv)*100:null;
      pnlEl.textContent=(bp>=0?'+':'')+'$'+Math.abs(bp).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
      pnlEl.className='card-value '+(bp>0?'long':bp<0?'short':'flat');
      pnlSub.textContent=(pct!==null?((pct>=0?'+':'')+pct.toFixed(2)+'% · '):'')+'realized by bot';
    } else {
      pnlEl.textContent='--'; pnlEl.className='card-value flat';
      pnlSub.textContent='realized by bot';
    }

    const pos=d.position||'FLAT';
    const pv=document.getElementById('pos-val');
    pv.textContent=pos; pv.className='card-value '+pos.toLowerCase();
    document.getElementById('pos-card').className='card'+(pos==='LONG'?' pos-long':pos==='SHORT'?' pos-short':'');
    document.getElementById('pos-sub').textContent=d.entry_price?'Entry @ $'+d.entry_price:'No open position';

    // trade events
    const tp=document.getElementById('trade-panel');
    document.getElementById('tc').textContent=d.trades.length;
    if(d.trades.length){
      tp.innerHTML=[...d.trades].reverse().map(t=>{
        const c=tagCls(t.msg);
        return`<div class="trade-row">
          <div class="trade-bar ${c}"></div>
          <div class="trade-body">
            <span class="trade-chip ${c}">${tagLabel(c)}</span>
            <div class="trade-text">${t.msg}</div>
            <div class="trade-ts">${t.ts} UTC</div>
          </div>
        </div>`;
      }).join('');
    } else {
      tp.innerHTML='<div class="empty"><div class="empty-icon">📊</div>No trade events yet today</div>';
    }

    // live log
    const lp=document.getElementById('log-panel');
    logBottom=lp.scrollTop+lp.clientHeight>=lp.scrollHeight-30;
    document.getElementById('lc').textContent=d.log_lines.length+' lines';
    if(d.log_lines.length){
      lp.innerHTML=d.log_lines.map(l=>`<div class="log-row"><span class="log-ts">${l.ts}</span><span class="log-msg ${l.level}">${l.msg}</span></div>`).join('');
      if(logBottom) lp.scrollTop=lp.scrollHeight;
    } else {
      lp.innerHTML='<div class="empty"><div class="empty-icon">📋</div>No logs yet</div>';
    }

    document.getElementById('foot-txt').textContent='Updated '+new Date().toLocaleTimeString();
  } catch(e){
    document.getElementById('foot-txt').textContent='Connection error — retrying...';
  }
}
refresh();
setInterval(refresh,5000);
</script>
</body>
</html>"""

TRADE_KEYWORDS = [
    "Y LONG filled", "Z SHORT filled", "STP3 filled", "Reverse filled",
    "Exit all", "Reverse flattened", "Y/Z OCO", "Candle open",
    "Risk exit", "noon exit", "3:59pm", "Session complete",
    "Early close", "ELV=", "Connected",
]

SKIP_LOGGERS = {"ibapi.wrapper", "ibapi.client", "ibapi.decoder", "ibapi.comm"}

LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(INFO|WARNING|ERROR)\] ([\w\.]+): (.+)$"
)


def parse_log():
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    state = {
        "account": None, "elv": None, "leg_qty": None,
        "candle_open": None, "entries": 0,
        "position": "FLAT", "pos_qty": None, "entry_price": None,
        "daily_pnl": None, "bot_pnl": None,
        "status": "stopped", "trades": [], "log_lines": [],
    }
    try:
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return state

    today_lines = [l for l in lines if l.startswith(today)]
    parsed = []
    seen = set()
    for line in today_lines:
        m = LOG_RE.match(line.strip())
        if not m:
            continue
        ts_full, level, logger, msg = m.group(1), m.group(2), m.group(3), m.group(4)
        ts = ts_full[11:19]
        key = (ts, msg)
        if key in seen:
            continue
        seen.add(key)
        if logger in SKIP_LOGGERS or msg.startswith("ANSWER ") or msg.startswith("REQUEST "):
            continue
        lvl = "warn" if level == "WARNING" else ("error" if level == "ERROR" else "info")
        parsed.append({"ts": ts, "level": lvl, "msg": msg})

    state["log_lines"] = parsed[-80:]

    for e in parsed:
        msg = e["msg"]
        if "Account:" in msg:
            state["account"] = msg.split("Account:")[-1].strip()
        if "ELV=" in msg:
            m2 = re.search(r"ELV=([\d.]+)", msg)
            if m2:
                state["elv"] = float(m2.group(1))
        if re.search(r"\bleg=(\d+)", msg):
            m2 = re.search(r"\bleg=(\d+)", msg)
            if m2:
                state["leg_qty"] = int(m2.group(1))
        if "Candle open:" in msg:
            m2 = re.search(r"Candle open: ([\d.]+)", msg)
            if m2:
                state["candle_open"] = m2.group(1)
                state["entries"] = 0
                state["position"] = "FLAT"
                state["entry_price"] = None
                state["pos_qty"] = None
        m_entry = re.search(r"entry#(\d+)", msg)   # entry# is on the fill lines
        if m_entry:
            state["entries"] = int(m_entry.group(1))
        if "LONG filled @" in msg:
            state["position"] = "LONG"
            if state["leg_qty"]:
                state["pos_qty"] = state["leg_qty"] * 2
            m2 = re.search(r"filled @ ([\d.]+)", msg)
            if m2:
                state["entry_price"] = m2.group(1)
        elif "SHORT filled @" in msg:
            state["position"] = "SHORT"
            if state["leg_qty"]:
                state["pos_qty"] = state["leg_qty"] * 2
            m2 = re.search(r"filled @ ([\d.]+)", msg)
            if m2:
                state["entry_price"] = m2.group(1)
        elif "Reverse entered: now" in msg:   # STP3 reverse flips the position
            m2 = re.search(r"now (\w+) (\d+)", msg)
            if m2:
                state["position"] = m2.group(1)
                state["pos_qty"] = m2.group(2)
        if ("Post-rev SL filled" in msg or "Exit all" in msg or "Exit only" in msg
                or "halt flatten" in msg or "Session complete" in msg):
            state["position"] = "FLAT"
            state["entry_price"] = None
            state["pos_qty"] = None
        if "PnL update:" in msg:
            m2 = re.search(r"PnL update: ([-\d.]+)", msg)
            if m2:
                state["daily_pnl"] = float(m2.group(1))
        if "botPnL=" in msg:
            m2 = re.search(r"botPnL=([-\d.]+)", msg)
            if m2:
                state["bot_pnl"] = float(m2.group(1))
        if any(k in msg for k in TRADE_KEYWORDS):
            state["trades"].append(e)

    if parsed:
        last = parsed[-1]["msg"]
        if "Waiting" in last or "pre-check" in last:
            state["status"] = "waiting"
        elif "Early close" in last:
            state["status"] = "holiday"
        elif "Session complete" in last:
            state["status"] = "ended"
        elif state["account"]:
            state["status"] = "running"

    return state


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/state")
def api_state():
    return jsonify(parse_log())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
