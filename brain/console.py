# ruff: noqa: E402
"""BreezeIQ console v2 — Room and Workbench, on their own port.

The console serves :8000, the only port. This module reuses the dashboard's
entire data and command layer — same `DashboardStore`, same POST facades, same
payload split between `/api/dashboard` and `/api/debug/dashboard` — and changes
only the HTML.

    Room      /        the room speaking for itself: one gauge, one control
    Workbench /debug   the wiring bench: click a part, see everything about it

Two independent templates rather than one stripped into the other. The old page
derived its public shell by regex over the debug shell, which coupled every
engineering edit to a public-surface rule; here each page fetches the endpoint
whose payload is already scoped for it, and the server-side split does the work.

    python3 console.py           # serve on BREEZEIQ_CONSOLE_PORT (default 8000)
"""
from __future__ import annotations

import os
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dashboard

DEFAULT_PORT = int(os.environ.get("BREEZEIQ_CONSOLE_PORT", "8000"))

# Shared by both pages. Kept in one string so Room and Workbench cannot drift
# into two different products, which is what happened to the tiles and captions
# on the original page.
_CSS = r'''

.zoommodal{position:fixed;inset:0;background:rgba(6,8,11,.6);backdrop-filter:blur(8px);display:flex;align-items:center;
  justify-content:center;z-index:50;padding:20px}
.zoommodal.hidden{display:none}
.zoomcard{background:rgba(22,27,34,.92);border:1px solid var(--line2);border-radius:20px;
  width:min(96vw,980px);max-height:92vh;overflow:auto;padding:18px 20px 20px;
  backdrop-filter:blur(24px) saturate(1.4);box-shadow:0 32px 80px rgba(0,0,0,.55);
  animation:modalin var(--med) var(--ease)}
@keyframes modalin{from{opacity:0;transform:scale(.97)}to{opacity:1;transform:scale(1)}}
.zoomhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}
.zoomhead h3{margin:0;font-size:16px}
.zoomtools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.zoomtools button{background:var(--surface2);border:1px solid var(--line2);color:var(--text);
  border-radius:var(--r-sm);padding:6px 12px;font-size:12.5px;cursor:pointer;transition:border-color var(--fast) var(--ease)}
.zoomtools button:hover:not(:disabled){border-color:rgba(90,200,250,.4)}
.zoomtools button:active:not(:disabled){transform:scale(.97)}
.zoomtools button:disabled{opacity:.35;cursor:not-allowed}
.zoomsub{color:var(--faint);font-size:12.5px;margin:2px 0 12px}
.zoomsvgwrap{position:relative;touch-action:none}
.zoombrush{fill:var(--cool-tint);stroke:rgba(90,200,250,.5);stroke-width:1}
.zoomcrosshair{stroke:var(--faint);stroke-width:1;stroke-dasharray:3 3}
.zoomtip{position:absolute;pointer-events:none;background:var(--surface2);border:1px solid var(--line2);
  border-radius:6px;padding:4px 8px;font-size:11.5px;color:var(--text);transform:translate(-50%,-115%);white-space:nowrap}
:root{
  /* ground — blue-black ladder, each step one legible layer above the last */
  --bg:#0A0D12;
  --surface:#14181F;
  --surface2:#1C222B;
  --inset:#0E1218;
  --line:rgba(255,255,255,.06);
  --line2:rgba(255,255,255,.11);

  /* ink */
  --text:#F5F7FA;
  --muted:#9CA7B4;
  --faint:#77828F;

  /* semantics — Apple dark-mode system palette, verbatim */
  --cool:#5AC8FA;    /* accent: interactive, selected, "the breeze" */
  --good:#32D74B;
  --warn:#FF9F0A;
  --bad:#FF453A;

  /* tints — the ONLY permitted translucent fills for pills/selection */
  --cool-tint:rgba(90,200,250,.14);
  --good-tint:rgba(50,215,75,.14);
  --warn-tint:rgba(255,159,10,.15);
  --bad-tint:rgba(255,69,58,.15);

  /* geometry */
  --r-sm:8px;   /* buttons, chips, small controls */
  --r-md:12px;  /* inner cards, inputs, sparkline wells */
  --r-lg:16px;  /* cards, modal */
  --r:14px;

  /* motion */
  --ease:cubic-bezier(.2,.8,.2,1);
  --fast:.15s;
  --med:.24s;

  /* Workbench board schematic only */
  --accent:#f0a84f;--pcb:#101b24;--pcb2:#152430;--gold:#c9a227;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",
  "Segoe UI Variable Text","Segoe UI",Roboto,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;background:var(--bg);color:var(--text);padding:0 0 70px}
#gTemp,.num,.bigval,.metric,.kv dd,.statchip b,.zoomsub{font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12.5px}
a{color:var(--cool)}
header{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  min-height:52px;padding:0 26px;background:rgba(10,13,18,.72);
  backdrop-filter:blur(20px) saturate(1.5);border-bottom:1px solid var(--line)}
.wordmark{font-size:19px;font-weight:600}.wordmark em{font-style:normal;color:var(--cool)}
.roomtag{color:var(--muted);font-size:13px;border-left:1px solid var(--line2);padding-left:14px}
.spacer{flex:1}
.seg{display:flex;background:var(--inset);border:1px solid var(--line);border-radius:999px;padding:2px}
.seg a{color:var(--muted);font:600 12.5px/1 inherit;padding:9px 20px;border-radius:999px;text-decoration:none;
  transition:background var(--fast) var(--ease),color var(--fast) var(--ease)}
.seg a[aria-current="page"]{background:var(--surface2);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.4)}
.seg a[aria-current="page"]:focus-visible{box-shadow:0 1px 3px rgba(0,0,0,.4),0 0 0 3px rgba(90,200,250,.4)}
main{max-width:1280px;margin:0 auto;padding:22px 26px}
.row{display:grid;gap:14px;margin-bottom:14px}
.cols-3{grid-template-columns:1fr 1fr 1fr}
.cols-insight{grid-template-columns:1.35fr 1fr}
/* The history and the jobs list share a row. The jobs card was the narrow
   column and had to wrap a standing instruction plus every outstanding job into
   it, so it read as squeezed beside a list that scrolls. Equal columns, and
   `align-items:start` so each card is as tall as its own content instead of
   being stretched to match the taller one. */
.cols-jobs{grid-template-columns:1fr 1fr;align-items:start}
@media(max-width:960px){.cols-jobs{grid-template-columns:1fr}}
@media(max-width:960px){.cols-3,.cols-insight{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:20px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
@media(max-width:720px){.card{padding:16px}}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.08em;color:var(--faint);text-transform:uppercase;margin-bottom:9px}
.sub{color:var(--muted);font-size:13px}
.pill{display:inline-block;font-size:11.5px;font-weight:600;border-radius:999px;padding:4px 11px;color:var(--muted);background:var(--surface2)}
.pill.good,.pill.live,.pill.acknowledged{color:var(--good);background:var(--good-tint)}
.pill.cool{color:var(--cool);background:var(--cool-tint)}
.pill.warn,.pill.stale,.pill.degraded,.pill.pending,.pill.held{color:var(--warn);background:var(--warn-tint)}
.pill.bad,.pill.failed,.pill.blocked,.pill.rejected{color:#FF6961;background:var(--bad-tint)}
.pill.dim{color:var(--muted);background:var(--surface2)}
/* WRAP, do not scroll. This was nowrap over overflow-x with the scrollbar
   hidden, so any control that did not fit was unreachable and nothing on
   screen admitted it — a fan with five speeds, Off and Auto overflowed a
   288px column even after the cards were widened. A second row of pills is
   legible; a button you cannot reach or see is not. */
.ctl{display:flex;gap:2px;flex-wrap:wrap;row-gap:2px;background:var(--surface2);border:1px solid var(--line2);border-radius:var(--r-md);padding:2px;margin-top:10px}
.ctl button{flex:1 0 auto}
/* A single digit does not need a word's padding, and six of them in one
   pill bar is the difference between fitting and scrolling invisibly. */
.ctl button.spd{padding-left:0;padding-right:0;min-width:32px;text-align:center}
.ctl button{border:0;background:none;color:var(--muted);font:600 12.5px/1 inherit;padding:11px 14px;border-radius:999px;cursor:pointer;
  transition:background var(--fast) var(--ease),color var(--fast) var(--ease)}
.ctl button.primary2{background:var(--cool-tint);color:var(--cool)}
.ctl button:disabled{opacity:.35;cursor:not-allowed}
.bench{margin-top:12px;border:1px solid var(--line2);border-radius:12px;padding:10px 12px;background:var(--surface2)}
.benchhead{font:600 11px/1.4 inherit;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:9px}
.benchrow{display:flex;align-items:center;gap:10px;margin-bottom:7px}
.benchlbl{flex:0 0 62px;font:600 11.5px/1 inherit;color:var(--muted)}
.benchbtns{display:flex;gap:4px;flex-wrap:wrap;flex:1}
.benchbtns button{border:1px solid var(--line2);background:var(--surface);color:var(--muted);
  font:600 12px/1 inherit;padding:8px 12px;border-radius:8px;cursor:pointer}
.benchbtns button:hover{border-color:var(--cool);color:var(--ink)}
.benchbtns button.primary2{background:var(--cool-tint);color:var(--cool);border-color:var(--cool)}
.benchnote{font:400 11.5px/1.5 inherit;color:var(--faint);margin-top:8px}
/* The two turn buttons are the point of the panel: filled, so they read as
   "press to act" rather than "currently selected" — the outlined pick
   buttons above them are the toggles. Stop is the red action. */
.benchbtns button.bgo{flex:1;padding:13px 10px;font-size:13.5px;border:0;
  color:#04121e;background:var(--cool);font-weight:700}
.benchbtns button.bgo:hover{filter:brightness(1.15)}
.benchbtns button.bstop{flex:1;padding:11px 10px;border:0;font-weight:700;
  color:#1a0603;background:var(--bad,#e0674f)}
.benchbtns button.bstop:hover{filter:brightness(1.12)}
.never{margin-top:9px;font-size:11.5px;color:var(--faint);display:block}
.never input{accent-color:var(--cool);margin-right:5px}
.expandbtn{padding:9px 16px;font-weight:600;flex:none;background:var(--surface2);border:1px solid var(--line2);color:var(--text);
  border-radius:var(--r-sm);padding:0 12px;font-size:12px;cursor:pointer;white-space:nowrap;
  transition:border-color var(--fast) var(--ease)}
.expandbtn:hover:not(:disabled){border-color:rgba(90,200,250,.4)}
.expandbtn:active:not(:disabled){transform:scale(.97)}
.expandbtn:disabled{opacity:.35;cursor:not-allowed}
@keyframes pulse{0%{opacity:.55}100%{opacity:1}}
.pulse{animation:pulse 1.2s ease-in-out infinite alternate}
.statebar{display:flex;align-items:center;gap:12px;width:fit-content;margin:14px 26px 0;padding:8px 16px;font-size:13px;
  background:var(--surface);border:1px solid var(--line);border-radius:999px;box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
.statebar::before{content:'';width:8px;height:8px;border-radius:50%;flex:none;background:var(--faint)}
.statebar[data-state="live"]::before{background:var(--good)}
.statebar[data-state="stale"]::before,.statebar[data-state="degraded"]::before{background:var(--warn)}
.statebar[data-state="failed"]::before{background:var(--bad)}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:20;
  background:var(--surface2);border:1px solid var(--line2);border-radius:var(--r-md);padding:11px 18px;
  font-size:13.5px;display:none;backdrop-filter:blur(20px) saturate(1.5)}
.toast.show{display:block;animation:toastin var(--med) var(--ease)}
@keyframes toastin{from{opacity:0;transform:translateX(-50%) translateY(8px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
.toast.bad{border-color:var(--bad);color:var(--bad)}
.toast.good{border-color:var(--good);color:var(--good)}
.empty{color:var(--faint);font-size:13px;padding:10px 0}
footer{max-width:1280px;margin:26px auto 0;padding:0 26px;color:var(--faint);font-size:12px;line-height:1.7}
:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(90,200,250,.4)}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{transition:none!important;animation:none!important}
}
'''

_ROOM_CSS = r'''
.primary{display:grid;grid-template-columns:auto 1fr auto;gap:30px;align-items:center}
@media(max-width:960px){.primary{grid-template-columns:1fr;justify-items:center;text-align:center}}
.gauge{width:228px}
.verdict{font-size:24px;font-weight:600;margin:0 0 6px}
.verdict .dot{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--good);margin-right:9px;box-shadow:0 0 12px rgba(50,215,75,.7)}
.herometa{display:flex;gap:9px;flex-wrap:wrap;margin-top:13px}
@media(max-width:960px){.herometa{justify-content:center}}
.setside{border-left:1px solid var(--line);padding-left:28px;min-width:250px}
@media(max-width:960px){.setside{border-left:0;padding-left:0;border-top:1px solid var(--line);padding-top:16px;width:100%}}
.setrow{display:flex;align-items:center;gap:14px;margin:10px 0 4px}
.stepbtn{width:52px;height:52px;border-radius:var(--r-sm);border:1px solid var(--line2);background:var(--surface2);color:var(--text);font-size:24px;cursor:pointer;
  transition:border-color var(--fast) var(--ease),transform var(--fast) var(--ease)}
.stepbtn:hover:not(:disabled){border-color:rgba(90,200,250,.4)}
.stepbtn:active:not(:disabled){transform:scale(.97)}
.stepbtn:disabled{opacity:.35;cursor:not-allowed}
.setval{font-size:22px;font-weight:700;flex:1;text-align:center}
.setval small{font-size:15px;color:var(--muted);font-weight:400}
.feelrow{display:flex;gap:10px;margin-top:12px}
.feelrow button{flex:1;border:1px solid var(--line2);background:var(--surface2);color:var(--text);font:600 13px/1 inherit;padding:12px;border-radius:var(--r-sm);cursor:pointer;
  transition:border-color var(--fast) var(--ease),transform var(--fast) var(--ease)}
.feelrow button:hover{border-color:rgba(90,200,250,.4)}
.feelrow button:active{transform:scale(.97)}
.plannerline{font-size:16.5px;font-weight:500;margin:6px 0 8px}
.dots{display:flex;gap:8px;align-items:center;margin:10px 0 4px;flex-wrap:wrap}
.dots i{width:12px;height:12px;border-radius:50%;background:rgba(50,215,75,.25);border:1.5px solid var(--good)}
.dots i.warn{background:var(--warn-tint);border-color:var(--warn)}
.dots .lbl{font-size:10.5px;color:var(--faint);margin:0 2px}
.rail{position:relative;height:6px;border-radius:999px;background:var(--inset);border:1px solid var(--line);margin:44px 6px 30px}
.rail .tick{position:absolute;top:14px;color:var(--faint);font-size:11px;transform:translateX(-50%)}
.rail .m{position:absolute;top:-8px;width:3px;height:22px;border-radius:1.5px;transform:translateX(-50%)}
.rail .m em{position:absolute;top:-24px;left:50%;transform:translateX(-50%);font:600 12.5px/1 inherit;font-style:normal;white-space:nowrap}
/* The two marks converge whenever cooling is off, so their labels sit on
   different lines rather than overlapping into an unreadable smear. */
.rail .m.actual{background:var(--bad)}
.rail .m.actual em{color:var(--bad);top:-42px}
.rail .m.cooled{background:var(--cool)}.rail .m.cooled em{color:var(--cool)}
.kwh{font-size:22px;font-weight:700;line-height:1}
.kwh small{font-size:15px;color:var(--muted);font-weight:400}
/* 210px fitted three word-buttons. A fan card now carries five speeds plus
   Auto, and .ctl is nowrap over a HIDDEN scrollbar — so whatever did not
   fit was simply unreachable, with nothing on screen saying so. */
.devgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(264px,1fr));gap:11px}
.dev{background:var(--inset);border:1px solid var(--line);border-radius:var(--r-md);padding:14px}
.dev .head{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.dev .name{font-weight:600;font-size:14px}
.dev .head .pill{margin-left:auto}
.dev .state{color:var(--muted);font-size:13px;margin-bottom:11px}
.holdrow{display:flex;align-items:center;gap:10px;margin-bottom:12px;color:var(--muted);font-size:13px;flex-wrap:wrap}
.holdrow select{background:var(--surface2);border:1px solid var(--line2);border-radius:var(--r-md);color:var(--text);padding:8px 12px;font:inherit;font-size:13px}
.chart{width:100%;height:auto;display:block;background:var(--inset);border-radius:var(--r-md)}
.chipsrow{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}
.statchip{background:var(--inset);border-radius:var(--r-sm);padding:6px 10px;font-size:12px;color:var(--faint)}
.statchip b{color:var(--text);font-size:15px;font-weight:700;margin-right:4px}
.legend{display:flex;gap:16px;color:var(--faint);font-size:11.5px;margin-top:8px;flex-wrap:wrap}
.chartheadrow{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.viewtabs{display:inline-flex;background:var(--inset);border:1px solid var(--line);border-radius:999px;padding:2px}
.viewtabs button{background:none;border:0;color:var(--muted);font:inherit;font-size:12.5px;font-weight:600;
  padding:5px 14px;border-radius:999px;cursor:pointer;transition:background var(--fast) var(--ease),color var(--fast) var(--ease)}
.viewtabs button[aria-selected=true]{background:var(--surface2);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.4)}
.viewtabs button[aria-selected=true]:focus-visible{box-shadow:0 1px 3px rgba(0,0,0,.4),0 0 0 3px rgba(90,200,250,.4)}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.insight{display:flex;gap:10px;align-items:flex-start;padding:10px 0 10px 12px;border-left:2px solid var(--cool);font-size:13px;color:var(--muted)}
.insight+.insight{border-top:1px solid var(--line);margin-top:2px}
.insight .ic{flex:none;font-size:15px;line-height:1.4}
.insight button{flex:none;align-self:center;background:var(--surface2);border:1px solid var(--line2);color:var(--text);
  font:600 12px/1 inherit;padding:7px 11px;border-radius:var(--r-sm);cursor:pointer}
.insight .pill{flex:none;align-self:center;margin-left:auto}
.insight b{color:var(--text);font-weight:600}
@media(max-width:720px){
  #gTemp{font-size:36px}
  .devgrid{grid-template-columns:1fr}
}
'''

_BENCH_CSS = r'''
.glance{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}
.glance .pill{font-size:12.5px;padding:8px 14px}
.glance .pill.click{cursor:pointer}
.engwrap{display:grid;grid-template-columns:1fr 350px;gap:14px;align-items:start}
@media(max-width:1000px){.engwrap{grid-template-columns:1fr}}
.engwrap>*{min-width:0}
.boardwrap{overflow-x:auto}
.board{width:100%;min-width:620px;height:auto;display:block}
.hot{cursor:pointer}
.hot .chipbox{fill:var(--surface2);stroke:var(--line2);transition:stroke var(--fast) var(--ease)}
.hot:hover .chipbox{stroke:var(--cool)}
.hot.sel .chipbox{stroke:var(--cool);stroke-width:1.5}
.hot text{pointer-events:none}
.hot:focus-visible{outline:none;box-shadow:none}
.hot:focus-visible rect:first-of-type{stroke:var(--cool);stroke-width:2}
.panel{position:sticky;top:100px}
.panel .card{padding:17px 19px}
.panel h3{font-size:16px;font-weight:600;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.panel .bigval{font-size:22px;font-weight:700;margin:8px 0 2px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:9px 16px;font-size:13px;margin-top:10px}
.kv dt{color:var(--faint);font-size:12px}.kv dd{text-align:right;font-size:13px}
.diag{margin-top:11px;font-size:12.5px;line-height:1.5;border-radius:var(--r-md);padding:9px 11px}
.diag.bad{color:var(--bad);background:var(--bad-tint)}
.diag.warn{color:var(--warn);background:var(--warn-tint)}
.diag.good{color:var(--good);background:var(--good-tint)}
.panelspark{width:100%;height:44px;background:var(--inset);border-radius:var(--r-md)}
.sparkrow{display:flex;align-items:stretch;gap:8px;margin-top:10px}
.sparkrow .panelspark{flex:1;margin-top:0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.actions a{border:1px solid var(--cool);color:var(--cool);font:600 12.5px/1 inherit;padding:9px 13px;border-radius:var(--r-sm);text-decoration:none}
.storagemini{font-size:12px;color:var(--faint);margin-top:11px;border-top:1px solid var(--line);padding-top:9px;word-break:break-word}
.declist{display:flex;flex-direction:column;gap:7px;margin-top:10px;max-height:300px;overflow-y:auto}
.decrow{display:flex;gap:10px;align-items:baseline;font-size:12.5px;padding:7px 10px;background:var(--inset);border-radius:var(--r-sm);border-left:3px solid var(--line2)}
.decrow.acted{border-left-color:var(--good)}
.decrow.flag{border-left-color:var(--warn)}
.decrow.bad{border-left-color:var(--bad)}
.decrow time{color:var(--faint);font-size:11.5px;min-width:52px}
.bars{display:flex;flex-direction:column;gap:8px;margin-top:12px}
.bar{display:grid;grid-template-columns:150px 1fr 74px;gap:12px;align-items:center;font-size:12.5px}
.bar .track{height:8px;border-radius:5px;background:var(--inset);overflow:hidden}
.bar .fill{height:100%;border-radius:5px;background:var(--cool);opacity:.7}
.bar .n{text-align:right;color:var(--muted)}
/* The actuation journal: five facts per command, in fixed columns, because
   scanning down "which of these was refused" is the reason to read it. */
.actlist{display:flex;flex-direction:column;gap:7px;margin-top:10px;max-height:340px;overflow-y:auto}
/* The Room card had six fixed rows and no scroller, so the history it
   promised was mostly unreachable. Same treatment as the bench list. */
#actions{max-height:320px;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.actrow{display:grid;grid-template-columns:62px 1fr 108px 118px;gap:12px;align-items:baseline;
  font-size:12.5px;padding:8px 10px;background:var(--inset);border-radius:var(--r-sm);
  border-left:3px solid var(--line2)}
.actrow.done{border-left-color:var(--good)}
.actrow.warn{border-left-color:var(--warn)}
.actrow.bad{border-left-color:var(--bad)}
.actrow time{color:var(--faint);font-size:11.5px}
.actrow .who{color:var(--muted)}
.actrow .out{text-align:right}
.actrow em{display:block;font-style:normal;color:var(--faint);font-size:11.5px;margin-top:3px}
@media(max-width:720px){.actrow{grid-template-columns:1fr}.actrow .out{text-align:left}}
/* R5: the AC card's number is arithmetic, so the arithmetic is shown. Each
   section is headed by what its values ARE — measured off a sensor, or
   computed by the model — because on that card the distinction is the claim. */
.calc{margin-top:11px;border:1px solid var(--line2);border-radius:var(--r-md);overflow:hidden}
.calcsec{padding:8px 11px;border-top:1px solid var(--line)}
.calcsec:first-child{border-top:0}
.calchead{font:600 10.5px/1.4 inherit;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);margin-bottom:5px}
.calcsec.measured .calchead{color:var(--good)}
.calcsec.modelled .calchead{color:var(--cool)}
.calcrow{display:flex;justify-content:space-between;gap:12px;font-size:12.5px;line-height:1.75}
.calcrow span:first-child{color:var(--muted)}
.calcnote{font-size:11.5px;color:var(--faint);padding:8px 11px;border-top:1px solid var(--line);line-height:1.5}
'''

# Shared JS: formatting, fetch plumbing, and the POST helpers. Both pages talk
# to the same API the original dashboard exposes.
_JS_COMMON = r'''
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const text=(id,v)=>{const e=$(id);if(e)e.textContent=v==null?'--':v};
const num=(v,d=1)=>v==null||isNaN(v)?'--':Number(v).toFixed(d);
const temp=v=>v==null?'--':`${num(v,1)} °C`;
const wh=v=>v==null?'--':`${num(v,0)} Wh`;
function age(s){if(s==null)return '--';s=Math.max(0,s);
  if(s<60)return `${Math.round(s)} s ago`;
  if(s<3600)return `${Math.round(s/60)} min ago`;
  return `${(s/3600).toFixed(1)} h ago`}
function pillClass(s){const ok=new Set(['live','stale','degraded','pending','failed','blocked','good','warn','bad','cool','dim']);
  return `pill ${ok.has(s)?s:'dim'}`}
let TOASTT=null;
function toast(msg,kind){const t=$('toast');if(!t)return;t.textContent=msg;t.className=`toast show ${kind||''}`;
  clearTimeout(TOASTT);TOASTT=setTimeout(()=>t.className='toast',3200)}
// ISO 7730's PPD from PMV — the payload publishes the index, not the share of
// people, and a share is the only form of it worth showing anybody.
function ppdOf(pmv){return pmv==null?null:
  100-95*Math.exp(-0.03353*Math.pow(pmv,4)-0.2179*Math.pow(pmv,2))}
function people(ppd){return ppd==null?'--':`about ${Math.max(1,Math.round(ppd))} in 100`}
// The board's own readback lags a real acknowledged command — HA takes a few
// seconds to report an entity it just accepted, and DeviceMonitor polls on
// its own cycle on top of that. Observed on the board: a tap came back
// acknowledged:true while the very next payload still reported the OLD
// state, so the pill and the highlight never moved and a working button
// read as broken. This is not solved by refreshing sooner — it is solved by
// not waiting on the readback for what the button itself just decided.
//
// OPTIMISTIC holds what the tap asked for, per device, until either the
// board's own reported value agrees (real confirmation always wins over a
// timer) or OPTIMISTIC_MS has passed with no confirmation, at which point the
// real state takes over rather than a stale guess lying forever. Shared
// between Room and Workbench, which both read device state off the same
// `d.devices` array and must not drift into two answers for one tap.
const OPTIMISTIC = {};
const OPTIMISTIC_MS = 15000;

function setOptimistic(device, field, value){
  OPTIMISTIC[device] = {field, value, until: Date.now() + OPTIMISTIC_MS};
}

function applyOptimistic(d){
  const list = (d && d.devices) || [];
  for (const key of Object.keys(OPTIMISTIC)) {
    const o = OPTIMISTIC[key], x = list.find(v => v.key === key);
    if (!x) continue;
    const real = x.reported && x.reported.state ? x.reported.state[o.field] : undefined;
    if (real === o.value) { delete OPTIMISTIC[key]; continue; }   // confirmed
    if (Date.now() > o.until) { delete OPTIMISTIC[key]; continue; }  // gave up
    x.reported = {...x.reported, available: 1,
      state: {...(x.reported && x.reported.state), [o.field]: o.value}};
  }
  return d;
}

// One place sends a manual command and marks it optimistic, so Room's buttons
// and Workbench's controls cannot drift into two behaviours for one tap.
async function sendManual(device, action, value, reason){
  const body = {confirm:true, device, action, ttl_min: ttl(), reason, value};
  const done = await post('/api/manual','manual-control', body);
  if (done && action === 'power') setOptimistic(device, 'power', value);
  if (done && action === 'speed') setOptimistic(device, 'speed', value);
  return done;
}

// Bench verbs travel the same audited endpoint as every other command, but
// carry no ttl and set nothing optimistic: a bench pulse is somebody proving
// a wire, not a standing instruction about the room.
async function sendBenchMotor(device, command, speed, ms){
  return await post('/api/manual','manual-control',
    {confirm:true, device, action:'bench_motor', command, speed, ms,
     reason:'bench motor test'});
}

// Device descriptors shared by Room's device cards and Workbench's per-part
// controls, so "the fan control on the bench" and "the fan control in Room"
// are one implementation rather than two that can drift apart.
const ICON={ac:'❄️',fan:'🌀',light:'💡',blinds:'🪟',window:'🔧'};
const NAME={ac:'Air conditioner',fan:'Ceiling fan',light:'Tubelight',
  blinds:'Blinds motor',window:'Spare motor'};
// No blinds entry: the module is off the rig, so the room is offered no card
// for it and therefore no standing preference either. The Workbench keeps it.
const NEVER={ac:'never switch this on for me',fan:'never run the fan',
  light:'never touch the tubelight'};
// D9 drives a motor with nothing attached to it. Commanding it changes no room,
// so it is not a device a person can be offered — it appears on the Workbench
// as the spare it is.
const HIDDEN=new Set(['window']);
const POWERED=new Set(['ac','fan','light']);
function label(x){return NAME[x.key]||x.label}
// `desired` is what the brain asked for; `reported` is what the device says
// about itself. They can disagree, and when they do the device's own word wins
// on this page — it is the one that describes the room.
function reported(x){return (x.reported&&x.reported.state)||null}
function isOn(x){
  const r=reported(x);
  if(r&&r.position)return r.position==='OPEN';
  if(r&&typeof r.power==='boolean')return r.power;
  return typeof x.desired==='number'?x.desired>0:!!x.desired}
// The ceiling fan is the only device behind a metered cloud API, so it is the
// only one whose "unavailable" has more than one cause: a fan off the network,
// a fan whose daily vendor budget is spent, and a fan with no account
// configured are three different problems with three different answers, and
// the readback detail cannot tell them apart. `fan_quota` names which it is.
// The three states below are the ones where no command can leave the board at
// all, each with the short word its card shows in place of "unreachable".
const QUOTA_BLOCKED={spent:'out of commands',unreadable:'budget unknown',
  unconfigured:'not set up'};
function fanQuota(x){
  return x.key==='fan'?((STATE&&STATE.fan_quota)||{}):{};
}
function stateText(x){
  const r=reported(x),avail=x.reported&&x.reported.available;
  if(avail===0)return fanQuota(x).state==='spent'
    ?'out of commands until midnight':(x.reported.detail||'not reachable');
  if(x.key==='fan'){const s=typeof x.desired==='number'?x.desired:0;
    return s>0?`running · speed ${s} of 6`:'off'}
  if(x.kind==='cover')return r&&r.position?(r.position==='OPEN'?'open':'shut'):'position unknown';
  return isOn(x)?'on':'off'}
// Why a device cannot be commanded right now, or "" when it can.
// The control process publishes the list of devices it will actually accept,
// and it is shorter than the list this page draws cards for. Rendering live
// buttons for the rest meant a tap that was silently rejected and a card that
// snapped back to its old state -- which reads as a broken page rather than as
// an unreachable device.
function whyBlocked(x, prefs){
  if(!(CAP&&CAP.available))return CAP&&CAP.detail?CAP.detail:'Controls are not ready';
  if(prefs&&prefs[`allow_${x.key}`]===false)
    return `You asked BreezeIQ never to use the ${label(x).toLowerCase()}`;
  if(CAP.devices&&!CAP.devices.includes(x.key))
    return 'This one is on automatic only — it has no manual control';
  // Before the generic answer, because "not reachable" was the only thing this
  // said for a fan whose command budget was simply spent.
  const q=fanQuota(x);
  if(QUOTA_BLOCKED[q.state])return q.message;
  if(x.reported&&x.reported.available===0)
    return x.reported.detail
      ? `Not reachable: ${x.reported.detail}`
      : 'Not answering right now, so a tap would not reach it';
  return '';
}

// ── the actuation journal, on both surfaces ─────────────────────────────
// Room and Workbench show the SAME history in different words, so the pill
// colour and the clock come from one place: a command that failed must not
// look green on one page and red on the other.
// Stripe colour per outcome. Shared because the same command must not read
// green on one page and grey on another, and the log page needs it too. Keys
// are the DISPLAY words from dashboard.ACTION_OUTCOMES: five of those used to
// collapse to "not sent", and each now has its own, so each needs a colour.
const ACT_STRIPE={done:'done',failed:'bad',refused:'bad','held back':'warn',
  'your hold':'warn','out of commands':'warn','too soon':'warn',
  'waiting to retry':'warn','not live':'',unrecorded:'warn',
  'not sent':'','unavailable':'warn'};
const OUTCOME_PILL={done:'good','not sent':'dim',failed:'bad',refused:'bad',
  'held back':'warn',unrecorded:'warn',unavailable:'dim'};
function actionTime(a){
  return a.at==null?'time not recorded'
    :new Date(a.at*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}
function actionName(a){return NAME[a.device]||a.device||'unknown device'}

// The pressed-looking button is the one matching what the device actually
// reports, never a fixed role. "On" carried class="primary2" unconditionally
// before this — a device sitting off still showed its On button lit, which is
// the literal complaint: click Off, and On keeps looking selected.
function devButtons(x, blocked){
  const dis=blocked?'disabled':'';
  if(x.key==='fan'){
    const speed=typeof x.desired==='number'?x.desired:0;
    // Every speed the fan actually has. It offered 1, 3 and 6 — and 6 is off
    // this fan's scale entirely, so the button that looked like "maximum"
    // addressed a speed that does not exist. Read the top from the payload
    // so the buttons follow the appliance rather than a literal here.
    const top=(STATE&&STATE.fan_speed_max)||5;
    return Array.from({length:top},(_,i)=>i+1).map(v=>`<button ${dis} class="spd${v===speed?' primary2':''}" data-dev="fan" data-act="speed" data-val="${v}">${v}</button>`).join('')
      +`<button ${dis} class="spd${speed===0?' primary2':''}" data-dev="fan" data-act="speed" data-val="0">Off</button>`
      +`<button ${dis} data-dev="fan" data-act="auto">Auto</button>`;
  }
  const on=isOn(x), [onLabel,offLabel]=x.kind==='cover'?['Open','Shut']:['On','Off'];
  return `<button ${on?'class="primary2"':''} ${dis} data-dev="${esc(x.key)}" data-act="power" data-val="true">${onLabel}</button>`
    +`<button ${!on?'class="primary2"':''} ${dis} data-dev="${esc(x.key)}" data-act="power" data-val="false">${offLabel}</button>`
    +`<button ${dis} data-dev="${esc(x.key)}" data-act="auto">Auto</button>`;
}

// Bench panel in the terms a bare motor actually has: which way it turns and
// how fast. "Open" and "Shut" are the blinds abstraction and they are useless
// on a bench where nothing is coupled to the shaft yet — a shaft turns
// clockwise or it does not, and that is the thing being tested.
//
// Clockwise is IA1 (the direction the ladder calls OPEN); anticlockwise is
// IA2. The wire protocol underneath is unchanged, only the label a person
// reads. Pick speed, pick how long, then turn it.
// Selection lives here, NOT in the DOM. The card re-renders on every poll
// (~10s), which used to wipe the highlighted speed and run time back to
// defaults mid-test -- so picking "10s" and pressing turn a moment later
// silently sent 3s instead. Anything a person picks has to outlive a repaint.
const BENCH={speed:100, ms:3000, hold:false, timer:null, note:'', ticker:null, runningUntil:0, runningWay:''};

function motorBench(){
  const speeds=[10,25,50,75,100];
  const runs=[[1000,'1s'],[3000,'3s'],[10000,'10s']];
  const on=(a,b)=>a===b?' primary2':'';
  const row=(label,html)=>`<div class="benchrow"><span class="benchlbl">${label}</span><span class="benchbtns">${html}</span></div>`;
  return `<div class="bench">
    <div class="benchhead">Motor control${(BENCH.timer||BENCH.ticker)?' · RUNNING':''}</div>
    ${row('1 · Speed', speeds.map(v=>
      `<button class="bspd${on(v,BENCH.speed)}" data-bspeed="${v}">${v}%</button>`).join(''))}
    ${row('2 · Run for', runs.map(([v,t])=>
      `<button class="bms${!BENCH.hold?on(v,BENCH.ms):''}" data-bms="${v}">${t}</button>`).join('')
      + `<button class="bms${BENCH.hold?' primary2':''}" data-bhold="1">Hold ∞</button>`)}
    ${row('3 · Turn', `<button class="bgo" data-bcmd="OPEN">↻ Clockwise</button><button class="bgo" data-bcmd="CLOSE">↺ Anticlockwise</button>`)}
    ${row('', `<button class="bstop" data-bcmd="STOP">■ Stop — coast</button><button class="bstop" data-bcmd="BRAKE">▣ Brake — hard</button>`)}
    <div class="benchnote" id="benchnote">${BENCH.note||'Pick a speed and a run time, then turn. <b>Hold ∞</b> keeps turning until you press Stop.'}</div>
  </div>`;
}

// Speed and duration are picks, not commands — they arm the next drive. Held
// in the DOM rather than a variable so a re-render cannot silently reset them
// to defaults while somebody is mid-test.
// Hold works the only way it can against a firmware that caps every run at
// 15s: re-issue before the current window expires. The cap stays honest --
// if this page closes or the network drops, the motor stops on its own
// within 15s rather than running unattended forever.
const HOLD_MS=15000, HOLD_REFRESH_MS=10000;

function benchStopHold(){
  if(BENCH.timer){clearInterval(BENCH.timer); BENCH.timer=null;}
  if(BENCH.ticker){clearInterval(BENCH.ticker); BENCH.ticker=null;}
  BENCH.runningUntil=0; BENCH.runningWay='';
}

// A timed run gets a live countdown driven by this page's own clock — the
// telemetry row lags a whole tick, and a bench operator needs to see the
// window they just bought counting down, not a 30s-old "coasting".
function benchCountdown(way, speed, ms){
  BENCH.runningUntil=Date.now()+ms; BENCH.runningWay=way;
  if(BENCH.ticker)clearInterval(BENCH.ticker);
  const tick=()=>{
    const left=BENCH.runningUntil-Date.now();
    if(left<=0){
      clearInterval(BENCH.ticker); BENCH.ticker=null;
      BENCH.runningUntil=0; BENCH.runningWay='';
      benchNote('Done — coasted to a stop on its own.');
      return;
    }
    benchNote(`<b>RUNNING ${way} at ${speed}%</b> — ${(left/1000).toFixed(1)}s left. Stop cuts it early.`);
  };
  tick(); BENCH.ticker=setInterval(tick,200);
}

function benchNote(text){
  BENCH.note=text;
  const n=document.querySelector('#benchnote');
  if(n) n.innerHTML=text;
}

function bindMotorBench(onDone){
  document.addEventListener('click', async e => {
    // ── picks: speed, run time, hold. Stored in BENCH so a repaint mid-test
    //    cannot silently revert them to defaults.
    const pick = e.target.closest('button[data-bspeed], button[data-bms], button[data-bhold]');
    if (pick){
      if(pick.dataset.bspeed) BENCH.speed=Number(pick.dataset.bspeed);
      else if(pick.dataset.bhold) BENCH.hold=true;
      else {BENCH.ms=Number(pick.dataset.bms); BENCH.hold=false;}
      const bench=pick.closest('.bench');
      const cls=pick.dataset.bspeed?'.bspd':'.bms';
      bench.querySelectorAll(cls).forEach(b=>b.classList.remove('primary2'));
      pick.classList.add('primary2');
      return;
    }
    const b = e.target.closest('button[data-bcmd]'); if (!b) return;
    const cmd = b.dataset.bcmd;
    // OPEN/CLOSE are the wire verbs; a person reading this panel thinks in
    // rotation, so every message they see is phrased that way.
    const turning = cmd==='OPEN'||cmd==='CLOSE';
    const way = cmd==='OPEN' ? 'clockwise' : 'anticlockwise';

    if(!turning){                       // Stop/Brake always cancel a hold too
      benchStopHold();
      benchNote(cmd==='BRAKE'?'braking…':'stopping…');
      const ok = await sendBenchMotor('blinds', cmd, null, null);
      benchNote(ok?(cmd==='BRAKE'?'Braked — hard stop, then released.':'Stopped — bridge cut, coasting.')
                  :'Refused — see the fault list.');
      if (ok && onDone) onDone();
      return;
    }

    benchStopHold();                    // a new direction supersedes any hold
    const speed = BENCH.speed;
    if(BENCH.hold){
      benchNote(`starting ${way} at ${speed}% — running until you press Stop…`);
      const ok = await sendBenchMotor('blinds', cmd, speed, HOLD_MS);
      if(!ok){benchNote('Refused — see the fault list.'); return;}
      benchNote(`<b>RUNNING ${way} at ${speed}% — holding.</b> Press Stop to end it.`);
      BENCH.timer=setInterval(()=>sendBenchMotor('blinds', cmd, speed, HOLD_MS),
                              HOLD_REFRESH_MS);
    } else {
      const ms = BENCH.ms;
      benchNote(`turning ${way} at ${speed}% for ${ms/1000}s…`);
      const ok = await sendBenchMotor('blinds', cmd, speed, ms);
      if(ok) benchCountdown(way, speed, ms);
      else benchNote('Refused — see the fault list.');
    }
    if (onDone) onDone();
  });
}

function bindDeviceTaps(onDone){
  document.addEventListener('click', async e => {
    const b = e.target.closest('button[data-dev]'); if (!b) return;
    const act = b.dataset.act;
    const value = act==='auto' ? null : act==='speed' ? Number(b.dataset.val) : b.dataset.val==='true';
    if (await sendManual(b.dataset.dev, act, value, 'dashboard tap')) {
      toast(act==='auto'?'Back to automatic':'Sent','good');
      if (onDone) onDone();
    }
  });
}


// One zoomable chart, shared by Room's "today on one clock" chart and every
// Workbench sensor's per-part trace. Built once so "pop this out and let me
// zoom" is answered the same way everywhere it is asked for, rather than as
// a bespoke widget per surface that would drift the moment one of them changed.
//
// `points` is a time-ordered array of {at, value}. `at` is a unix seconds
// timestamp -- required, because a chart with no real time axis is the
// complaint this exists to fix ("not time concentrated").
let ZOOM = null;  // {points, unit, title, start, end} while the modal is open

function ensureZoomModal(){
  if ($('zoomModal')) return;
  const div = document.createElement('div');
  div.id = 'zoomModal'; div.className = 'zoommodal hidden';
  div.innerHTML = `<div class="zoomcard">
    <div class="zoomhead"><h3 id="zoomTitle"></h3>
      <div class="zoomtools">
        <button id="zoomOut" title="Zoom out one step">− zoom</button>
        <button id="zoomReset">Full range</button>
        <button id="zoomClose">Close</button>
      </div></div>
    <p class="zoomsub" id="zoomSub"></p>
    <div class="zoomsvgwrap"><svg id="zoomSvg" viewBox="0 0 900 380" role="img"></svg></div>
  </div>`;
  document.body.appendChild(div);
  $('zoomClose').onclick = closeZoomModal;
  div.addEventListener('click', e => { if (e.target === div) closeZoomModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeZoomModal(); });
  $('zoomReset').onclick = () => { ZOOM.start = 0; ZOOM.end = ZOOM.points.length - 1; drawZoom(); };
  $('zoomOut').onclick = () => {
    const span = ZOOM.end - ZOOM.start, grow = Math.round(span * 0.5) || 1;
    ZOOM.start = Math.max(0, ZOOM.start - grow);
    ZOOM.end = Math.min(ZOOM.points.length - 1, ZOOM.end + grow);
    drawZoom();
  };
}

function closeZoomModal(){ const m = $('zoomModal'); if (m) m.classList.add('hidden'); ZOOM = null; }

// title: card heading. points: [{at,value}], oldest first. unit: appended
// after each value in the tooltip and the range chip (e.g. "°C", "lx", "").
function openZoomChart(title, points, unit){
  points = (points || []).filter(p => p && p.at != null && p.value != null);
  if (points.length < 2) { toast('Not enough history to chart yet', 'warn'); return; }
  ensureZoomModal();
  ZOOM = { points, unit: unit || '', title, start: 0, end: points.length - 1 };
  $('zoomModal').classList.remove('hidden');
  drawZoom();
}

function drawZoom(){
  const { points, unit, title, start, end } = ZOOM;
  const view = points.slice(start, end + 1);
  text('zoomTitle', title);
  const t0 = view[0].at, t1 = view[view.length - 1].at, span = Math.max(1, t1 - t0);
  const w = 900, h = 380, left = 54, right = w - 16, top = 16, bottom = h - 40;
  const vals = view.map(p => p.value), lo = Math.min(...vals), hi = Math.max(...vals), vspan = (hi - lo) || 1;
  const X = t => left + (t - t0) / span * (right - left);
  const Y = v => bottom - (v - lo) / vspan * (bottom - top);

  // Date-aware axis: a zoomed-in window gets time-of-day, a full day or more
  // gets a date -- the exact "not time concentrated" complaint is that a
  // full day squeezed onto one label spacing tells you nothing about WHEN.
  const spanH = span / 3600, showDate = spanH > 20;
  const ticks = [0, .2, .4, .6, .8, 1].map(f => {
    const t = t0 + span * f;
    const d = new Date(t * 1000);
    const label = showDate ? d.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' + d.toLocaleTimeString([], {hour:'2-digit'})
                            : d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    return `<text x="${X(t).toFixed(1)}" y="${h-14}" text-anchor="middle" fill="#77828F" font-size="11">${esc(label)}</text>`;
  }).join('');
  const yticks = [0,.25,.5,.75,1].map(f => {
    const v = lo + vspan * f;
    return `<text x="${left-8}" y="${Y(v)+4}" text-anchor="end" fill="#77828F" font-size="11">${num(v,1)}</text>`
      + `<line x1="${left}" y1="${Y(v).toFixed(1)}" x2="${right}" y2="${Y(v).toFixed(1)}" stroke="rgba(255,255,255,.05)" stroke-width="1"/>`;
  }).join('');
  const line = `<polyline fill="none" stroke="#5AC8FA" stroke-width="2" points="${
    view.map(p => `${X(p.at).toFixed(1)},${Y(p.value).toFixed(1)}`).join(' ')}"/>`;

  const svg = $('zoomSvg');
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = yticks + line + ticks
    + `<rect id="zoomBrushArea" x="${left}" y="${top}" width="${right-left}" height="${bottom-top}" fill="transparent"/>`
    + `<rect id="zoomBrushRect" class="zoombrush" x="0" y="${top}" width="0" height="${bottom-top}" style="display:none"/>`
    + `<line id="zoomCrosshair" class="zoomcrosshair" x1="0" y1="${top}" x2="0" y2="${bottom}" style="display:none"/>`;

  const zoomed = start > 0 || end < points.length - 1;
  text('zoomSub', `${view.length} points · ${num(lo,1)}${unit} to ${num(hi,1)}${unit}`
    + (zoomed ? ' · zoomed — drag to zoom further, or Full range to reset' : ' · drag across the chart to zoom in'));
  $('zoomOut').disabled = !zoomed;
  $('zoomReset').disabled = !zoomed;

  bindZoomDrag(view, X, left, right, top, bottom, unit);
}

// Drag-to-zoom plus a hover tooltip, both driven off one pointer handler so a
// short drag (a tap) shows the value under the finger instead of a phantom
// zoom on a one-pixel selection.
function bindZoomDrag(view, X, left, right, top, bottom, unit){
  const svg = $('zoomSvg'), area = $('zoomBrushArea'), brush = $('zoomBrushRect'), cross = $('zoomCrosshair');
  let dragStart = null;
  const toSvgX = clientX => {
    const rect = svg.getBoundingClientRect();
    const px = (clientX - rect.left) / rect.width * 900;
    return Math.max(left, Math.min(right, px));
  };
  const nearestIndex = svgX => {
    let best = 0, bestDist = Infinity;
    view.forEach((p, i) => { const d = Math.abs(X(p.at) - svgX); if (d < bestDist) { bestDist = d; best = i; } });
    return best;
  };
  const showTip = (svgX, i) => {
    cross.setAttribute('x1', X(view[i].at)); cross.setAttribute('x2', X(view[i].at)); cross.style.display = '';
    let tip = document.getElementById('zoomTipEl');
    if (!tip) { tip = document.createElement('div'); tip.id = 'zoomTipEl'; tip.className = 'zoomtip';
      $('zoomModal').querySelector('.zoomsvgwrap').appendChild(tip); }
    const rect = svg.getBoundingClientRect();
    tip.style.left = (rect.width * X(view[i].at) / 900) + 'px';
    tip.style.top = (rect.height * top / 900) + 'px';
    tip.textContent = `${new Date(view[i].at*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})} · ${num(view[i].value,2)}${unit}`;
  };
  const hideTip = () => { const t = document.getElementById('zoomTipEl'); if (t) t.remove(); cross.style.display = 'none'; };
  area.onpointerdown = e => { dragStart = toSvgX(e.clientX); brush.style.display = ''; brush.setAttribute('x', dragStart); brush.setAttribute('width', 0); };
  area.onpointermove = e => {
    const svgX = toSvgX(e.clientX);
    if (dragStart != null) {
      const x = Math.min(dragStart, svgX), w = Math.abs(svgX - dragStart);
      brush.setAttribute('x', x); brush.setAttribute('width', w);
    } else { showTip(svgX, nearestIndex(svgX)); }
  };
  area.onpointerup = e => {
    const svgX = toSvgX(e.clientX);
    if (dragStart != null && Math.abs(svgX - dragStart) > 8) {
      const i0 = nearestIndex(Math.min(dragStart, svgX)), i1 = nearestIndex(Math.max(dragStart, svgX));
      ZOOM.start += i0; ZOOM.end = ZOOM.start + (i1 - i0);
      drawZoom();
    }
    dragStart = null; brush.style.display = 'none';
  };
  area.onpointerleave = () => { hideTip(); };
}


async function post(path,intent,body){
  const r=await fetch(path,{method:'POST',
    headers:{'Content-Type':'application/json','X-BreezeIQ-Intent':intent},
    body:JSON.stringify(body)});
  const d=await r.json().catch(()=>({}));
  if(!r.ok||d.ok===false){toast(d.detail||d.error||'Rejected','bad');return null}
  return d}
function statebar(d){
  const b=$('statebar');if(!b)return;
  b.dataset.state=d.data_state||'failed';
  const t={live:'Live board data',stale:'Data is stale',degraded:'Safe degraded mode',failed:'Live data unavailable'};
  text('stateTitle',t[d.data_state]||'Unknown');
  text('stateDetail',d.error||(d.sample?`${d.sample.source||'board'} · sample ${age(d.sample.age_s)}`:'No sample'))}
'''

# ── Room ────────────────────────────────────────────────────────────────
ROOM_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BreezeIQ — Room</title>
<style>''' + _CSS + _ROOM_CSS + r'''</style></head>
<body>
<header>
  <span class="wordmark">Breeze<em>IQ</em></span>
  <span class="roomtag" id="roomTag">Loading</span>
  <span class="spacer"></span>
  <nav class="seg"><a href="/" aria-current="page">Room</a><a href="/workbench">Workbench</a><a href="/log">Log</a></nav>
</header>
<div class="statebar" id="statebar" data-state="failed">
  <strong id="stateTitle">Connecting</strong><span class="sub" id="stateDetail"></span>
</div>

<main>
<div class="row"><div class="card primary">
  <svg class="gauge" viewBox="0 0 220 132" role="img" aria-labelledby="gaugeTitle">
    <title id="gaugeTitle">Room temperature against the comfort window</title>
    <path d="M18,110 A92,92 0 0 1 202,110" fill="none" stroke="var(--line2)" stroke-width="10" stroke-linecap="round"/>
    <path id="gBand" fill="none" stroke="rgba(50,215,75,.25)" stroke-width="10" stroke-linecap="round" opacity=".85"/>
    <circle id="gDrift" r="5" fill="none" stroke="var(--bad)" stroke-width="2" opacity=".8"/>
    <line id="gNeedle" stroke="#F5F7FA" stroke-width="3" stroke-linecap="round"/>
    <text x="14" y="128" fill="#77828F" font-size="11">15°</text>
    <text x="192" y="128" fill="#77828F" font-size="11">35°</text>
    <text id="gTemp" x="110" y="86" text-anchor="middle" fill="#F5F7FA" font-size="44" font-weight="700" letter-spacing="-0.02em">--</text>
    <text id="gBandLabel" x="110" y="106" text-anchor="middle" fill="#9CA7B4" font-size="11"></text>
  </svg>
  <div>
    <div class="verdict"><span class="dot" id="verdictDot"></span><span id="verdictWord">--</span></div>
    <p class="sub"><span id="ppdLine"></span><br><span id="runLine"></span><br>
      <span style="color:var(--faint)" id="driftLine"></span></p>
    <div class="herometa" id="heroMeta"></div>
  </div>
  <div class="setside">
    <div class="eyebrow">Set where you like it</div>
    <div class="setrow">
      <button class="stepbtn" id="setDown" aria-label="cooler">−</button>
      <div class="setval num"><span id="setVal">--</span><small>°C</small></div>
      <button class="stepbtn" id="setUp" aria-label="warmer">+</button>
    </div>
    <p class="sub" style="text-align:center" id="setHint">BreezeIQ holds ±1° around this</p>
    <div class="feelrow">
      <button id="feelWarm">I feel warm</button><button id="feelCold">I feel cold</button>
    </div>
  </div>
</div></div>

<div class="row cols-3">
  <div class="card">
    <div class="eyebrow">Next hour</div>
    <div class="plannerline" id="planLine">--</div>
    <div class="dots" id="planDots" aria-label="Five-minute checks across the next hour"></div>
    <p class="sub" id="planSub"></p>
  </div>
  <div class="card">
    <div class="eyebrow">What cooling is doing</div>
    <div class="rail">
      <div class="m cooled" id="markWith"><em id="markWithLabel"></em></div>
      <div class="m actual" id="markWithout"><em id="markWithoutLabel"></em></div>
      <span class="tick" style="left:0">15°</span><span class="tick" style="left:50%">25°</span><span class="tick" style="left:100%">35°</span>
    </div>
    <p class="sub" style="text-align:center" id="railVerdict"></p>
    <p class="sub" style="text-align:center;margin-top:8px;color:var(--faint)" id="railNote"></p>
  </div>
  <div class="card">
    <div class="eyebrow">Energy</div>
    <div class="kwh num"><span id="savedVal">--</span> <small>saved today</small></div>
    <p class="sub" style="margin-top:4px" id="savedBasis"></p>
    <div class="chipsrow num" id="energyChips"></div>
  </div>
</div>

<div class="row cols-jobs">
  <div class="card">
    <div class="eyebrow">What BreezeIQ did</div>
    <div id="actions"><div class="empty">Nothing has been sent to a device yet</div></div>
  </div>
  <div class="card">
    <div class="eyebrow">What you need to do</div>
    <p class="sub" id="windowNote"></p>
    <div id="handWork"></div>
  </div>
</div>

<div class="row">
  <div class="card">
    <div class="eyebrow">Devices</div>
    <div class="holdrow">A manual change holds for
      <select id="holdFor">
        <option value="30" selected>30 minutes</option><option value="60">1 hour</option>
        <option value="120">2 hours</option><option value="until_auto">until I say</option>
      </select>
      <span id="capPill" class="pill dim">checking controls</span>
    </div>
    <div class="devgrid" id="devices"><div class="empty">Waiting for device evidence</div></div>
  </div>
</div>

<div class="row"><div class="card">
  <div class="chartheadrow">
    <div class="eyebrow">Today, on one clock</div>
    <div class="viewtabs" role="tablist" aria-label="What to plot">
      <button role="tab" id="tab-temp" aria-selected="true" data-view="temp">Temperature</button>
      <button role="tab" id="tab-comfort" aria-selected="false" data-view="comfort">Comfort</button>
    </div>
    <button id="expandDayChart" class="expandbtn" title="Open full history, zoomable">⤢ Expand</button>
  </div>
  <svg class="chart" id="chart" viewBox="0 0 1040 260" role="img" aria-label="The room over the last day"></svg>
  <div class="legend" id="chartLegend"></div>
  <div class="chipsrow num" id="dayChips"></div>
</div></div>

<p class="sub">Runs on the board in this room · nothing leaves but an encrypted nightly backup ·
  <a href="/about">how it works</a></p>
</main>
<div class="toast" id="toast"></div>
<footer id="foot"></footer>

<script>''' + _JS_COMMON + r'''
let STATE=null,CAP=null;
const LO=15,HI=35;                                  // the fixed rail, both views
// The band the room promises, in degrees, and the ISO 7730 Category B edge in
// PMV. One number each, used by the setpoint hint and both chart views, so the
// promise on the dial and the shading on the chart cannot drift apart.
const COMFORT_BAND_C=1.0, ISO_B=0.5;
const pct=c=>Math.max(0,Math.min(100,(c-LO)/(HI-LO)*100));
// The gauge sweeps 180°, left to right, over the same fixed 15–35 rail so the
// needle means the same thing between visits.
function polar(c){const a=Math.PI*(1-Math.max(0,Math.min(1,(c-LO)/(HI-LO))));
  return [110+92*Math.cos(a),110-92*Math.sin(a)]}

function renderHero(d){
  const c=d.comfort||{},ac=d.ac||{},room=c.indoor_c;
  const [nx,ny]=polar(room==null?LO:room);
  $('gNeedle').setAttribute('x1',110+((nx-110)*.35));
  $('gNeedle').setAttribute('y1',110+((ny-110)*.35));
  $('gNeedle').setAttribute('x2',nx);$('gNeedle').setAttribute('y2',ny);
  $('gTemp').textContent=room==null?'--':`${num(room,1)}°`;
  const set=ac.setpoint_c,band=COMFORT_BAND_C;
  if(set!=null){
    const [ax,ay]=polar(set-band),[bx,by]=polar(set+band);
    $('gBand').setAttribute('d',`M${ax.toFixed(1)},${ay.toFixed(1)} A92,92 0 0 1 ${bx.toFixed(1)},${by.toFixed(1)}`);
    // "Aiming for", not "comfort window". How a room FEELS is PMV, which also
    // depends on humidity and air movement, so the room can sit a little
    // outside this arc and still be comfortable. Calling the arc a comfort
    // window made the page contradict itself whenever that happened.
    $('gBandLabel').textContent=`aiming for ${num(set-band,1)} – ${num(set+band,1)}°`;
    text('setVal',num(set,0));
    // Say which rule ends this number. An indefinite hold nobody can see is
    // how a stale setpoint becomes next month's mystery, and an expiring one
    // that says nothing is what made the dial look like it reverted by itself.
    // The sentence is the server's (ac.setpoint.detail) so the page cannot
    // invent a second story about the same row.
    const sp=ac.setpoint||{};
    if(sp.detail)text('setHint',sp.detail);
  }else{$('gBand').setAttribute('d','');$('gBandLabel').textContent=''}
  // Where the room would sit with no cooling: the appliance's own reduction,
  // added back. Coincides with the needle whenever nothing is running.
  const drift=(room==null||ac.reduction_c==null)?null:room+Math.max(0,ac.reduction_c);
  if(drift!=null&&Math.abs(drift-room)>=0.1){
    const [dx,dy]=polar(drift);
    $('gDrift').setAttribute('cx',dx);$('gDrift').setAttribute('cy',dy);
    $('gDrift').style.display='';
    text('driftLine',`The red ring marks ${num(drift,1)}° — where it would drift without cooling.`);
  }else{$('gDrift').style.display='none';text('driftLine','')}

  const st=c.state||'unknown',warm=/warm|hot/i.test(st),cold=/cool|cold/i.test(st);
  text('verdictWord',warm?'A little warm':cold?'A little cool':'Comfortable');
  $('verdictDot').style.background=warm||cold?'var(--warn)':'var(--good)';
  $('verdictDot').style.boxShadow=`0 0 12px ${warm||cold?'rgba(255,159,10,.7)':'rgba(50,215,75,.7)'}`;
  // The needle carries the verdict too, so the dial and the words agree even
  // when the room sits outside the arc it is aiming for.
  $('gNeedle').setAttribute('stroke',warm||cold?'var(--warn)':'#F5F7FA');
  const ppd=ppdOf(c.pmv);
  text('ppdLine',ppd==null?'':`${people(ppd)} people would want it ${cold?'warmer':'cooler'}.`);
  // "Running" means drawing power, which is why this filters on POWERED
  // rather than on everything that reports itself as on.
  const on=(d.devices||[]).filter(x=>POWERED.has(x.key)&&isOn(x)).map(label);
  text('runLine',on.length?`Running: ${on.join(', ')}`
                          :'Nothing is running — the room is holding this on its own at 0 W.');
  const o=d.occupancy||{},meta=[];
  // Occupancy leads, and says HOW MANY when the camera counted. A count is
  // only shown when the camera is actually reporting: PIR knows somebody is
  // here but not how many, and printing "1 person" off a motion pin would be a
  // number invented from a boolean.
  const counted=o.camera_health==='ok'&&typeof o.count==='number'?o.count:null;
  meta.push(o.occupant==='AWAY'?['dim','Room is empty']
    :o.occupant==='ASLEEP'?['cool',counted!=null&&counted>1?`Asleep · ${counted} here`:'Asleep']
    :counted==null?['good',"You're here"]
    :counted===0?['dim','Nobody seen']
    :counted===1?['good','1 person here']
    :['good',`${counted} people here`]);
  if(c.outdoor_c!=null)meta.push(['dim',`Outside ${num(c.outdoor_c,1)}°`]);
  if(c.indoor_rh!=null)meta.push(['dim',`Humidity ${num(c.indoor_rh,0)}%`]);
  $('heroMeta').innerHTML=meta.map(([k,t])=>`<span class="${pillClass(k)}">${esc(t)}</span>`).join('');
}

// The board publishes the worst moment across the hour, not a value per step,
// so this shows exactly that: one bar, the limit marked, no invented curve.
function renderPlan(d){
  const p=d.plan||{},worst=p.worst_ppd,limit=p.band_ppd||10;
  if(!p.available){
    text('planLine','Looking ahead is warming up.');
    $('planDots').innerHTML='';
    text('planSub',p.message||p.reason||'No projection recorded yet.');
  }else{
    const held=worst!=null&&worst<=limit;
    text('planLine',held
      ?`Comfort holds for the next hour at ${wh(p.watt_hours||0)}.`
      :'The plan is deferring to what the sensors measure right now.');
    // A bar, not a dial: the only two numbers that exist are "worst ahead" and
    // "the limit", and their relationship is the whole message.
    const frac=worst==null?0:Math.min(1,worst/Math.max(limit*2,1));
    $('planDots').innerHTML=
      `<svg viewBox="0 0 300 26" style="width:100%;height:26px">
        <rect x="0" y="9" width="300" height="8" rx="4" fill="var(--inset)"/>
        <rect x="0" y="9" width="${(frac*300).toFixed(1)}" height="8" rx="4"
              fill="${held?'var(--good)':'var(--warn)'}"/>
        <line x1="150" y1="4" x2="150" y2="22" stroke="var(--muted)" stroke-width="1.5"/>
        <text x="154" y="8" fill="#77828F" font-size="9">limit</text></svg>`;
    text('planSub',worst==null?(p.reason||'')
      :`Worst moment ahead: ${people(worst)} dissatisfied — your window allows ${Math.round(limit)} in 100.`);
  }
}

// What the system just did, and why. Every line is a journalled command, so
// there is nothing to invent: a command whose outcome was never recorded says
// so, and a row with no clock says that too rather than reading as "just now".
function renderActions(d){
  const list=(d.actions||[]).slice(0,25);   // scrolls, so it is not capped at a screenful
  $('actions').innerHTML=list.length?list.map(a=>{
    const ended=a.outcome_detail?` — ${esc(a.outcome_detail)}`:'';
    return `<div class="insight"><div class="ic">${ICON[a.device]||'⚙️'}</div>
      <div><b>${esc(actionName(a))} ${esc(a.what)}</b> at ${esc(actionTime(a))}<br>
        <span style="color:var(--faint)">${esc(a.why||'no reason was recorded')}${ended}${a.repeated>1?` &middot; ${a.repeated}\u00d7`:''}</span></div>
      <span class="${pillClass(OUTCOME_PILL[a.outcome]||'dim')}">${esc(a.outcome)}</span>
    </div>`}).join(''):'<div class="empty">Nothing has been sent to a device yet.</div>';
}

// The two things only a person in the room can do. The window instruction is
// standing — it replaces a blinds control this room no longer has — and the
// second line appears only when BreezeIQ genuinely cannot reach a device
// itself, with the reason it cannot rather than a bare "unreachable".
function renderHandWork(d){
  text('windowNote',d.window_instruction||'');
  const out=[],q=d.fan_quota||{},prefs=d.preferences||{};
  // A spent command budget needs a hand on the fan, so it is a job, not a
  // notice.
  if(QUOTA_BLOCKED[q.state])
    out.push(['🌀',esc(q.message||''),null,null]);
  // A device you vetoed is only ever un-vetoed by you. This used to sit under
  // "BreezeIQ noticed", which is where it went unread: it is the one item on
  // that card that needed a person, next to three that did not.
  (prefs.options&&prefs.options.vetoable||[]).forEach(key=>{
    if(prefs[`allow_${key}`]!==false||HIDDEN.has(key))return;
    out.push(['🚫',`You asked BreezeIQ never to use the ${esc((NAME[key]||key).toLowerCase())}. `
      +`The plan routes around it, which can mean reaching for cooling where that `
      +`would have been enough.`,'Allow again',key]);
  });
  $('handWork').innerHTML=out.map(([ic,t,btn,key])=>
    `<div class="insight" style="border-left-color:var(--warn);margin-top:12px">
       <div class="ic">${ic}</div><div>${t}</div>
       ${btn?`<button data-unveto="${esc(key)}">${esc(btn)}</button>`:''}</div>`).join('');
}

// Below this percent-of-rail separation, two centered labels physically
// overlap: each is several characters wide and anchored by its own midpoint,
// with no allowance for the other. A 0.1 C reduction on a 20-degree rail
// puts the marks 0.5% apart -- both real, both shown, and their text
// collided into unreadable overlaid words rather than two temperatures.
const RAIL_LABEL_CLEAR_PCT = 9;

function renderRail(d){
  const c=d.comfort||{},ac=d.ac||{},room=c.indoor_c;
  const red=ac.reduction_c==null?0:Math.max(0,ac.reduction_c);
  if(room==null){text('railVerdict','Waiting for a room reading');return}
  const without=room+red;
  const withPct=pct(room), withoutPct=pct(without), close=Math.abs(withPct-withoutPct)<RAIL_LABEL_CLEAR_PCT;
  // The tick marks stay exactly where the numbers say. Only the text nudges
  // apart, and only when the marks are close enough to need it -- two marks
  // 10% of the rail apart already clear each other with no help.
  $('markWith').style.left=withPct+'%';$('markWithout').style.left=withoutPct+'%';
  $('markWithLabel').style.transform=close?'translateX(calc(-50% - 30px))':'';
  $('markWithoutLabel').style.transform=close?'translateX(calc(-50% + 30px))':'';
  text('markWithLabel',`with · ${num(room,1)}°`);
  text('markWithoutLabel',`without · ${num(without,1)}°`);
  $('markWithout').style.display=red>=0.1?'':'none';
  text('railVerdict',red>=0.1?`Keeping the room ${num(red,1)}° cooler than it would drift.`
                             :'Nothing running — the room is holding this by itself.');
  text('railNote',red>=0.1?'Both marks sit together whenever cooling is off.':'');
}

function renderEnergy(d){
  const e=d.energy||{},x=e.estimate||{};
  text('savedVal',x.available?wh(x.saved_wh):'--');
  text('savedBasis',x.available
    ?`vs cooling alone · estimate, ${num(x.coverage_pct,0)}% of today observed`
    :(x.message||'Waiting for a covered interval'));
  const chips=[];
  if(x.used_wh!=null)chips.push([wh(x.used_wh),'used today']);
  if(x.comfortable_pct!=null)chips.push([`${num(x.comfortable_pct,0)}%`,'time comfortable']);
  if(e.measured_wh!=null)chips.push([wh(e.measured_wh),'metered']);
  // "Holding the next hour for nothing" is the whole passive-first claim, so it
  // belongs beside the saving it produces. It used to be a fourth item on
  // "BreezeIQ noticed", one card away from the number it was talking about.
  const p=d.plan||{};
  if(p.available&&p.priced&&p.watt_hours===0&&p.worst_ppd<=(p.band_ppd||10))
    chips.push(['0 Wh','next hour, free']);
  $('energyChips').innerHTML=chips.map(([b,t])=>`<span class="statchip"><b>${esc(b)}</b>${esc(t)}</span>`).join('');
}

function renderDevices(d){
  const prefs=d.preferences||{};
  const list=(d.devices||[]).filter(x=>x.physical_present!==false&&!HIDDEN.has(x.key));
  $('devices').innerHTML=list.length?list.map(x=>{
    const vetoed=prefs[`allow_${x.key}`]===false;
    const unreachable=x.reported&&x.reported.available===0;
    const blocked=whyBlocked(x,prefs);
    // A spent command budget must not read "unreachable" — that is the whole
    // complaint. The quota word goes first because it also explains why the
    // readback is unavailable in the first place.
    const quota=QUOTA_BLOCKED[fanQuota(x).state];
    const pill=vetoed?['bad','never']:quota?['warn',quota]
      :unreachable?['warn','unreachable']
      :blocked?['warn','automatic only']
      :x.override?['pending','held']:['dim','auto'];
    return `<div class="dev"><div class="head">${ICON[x.key]||'⚙️'}
      <span class="name">${esc(label(x))}</span><span class="${pillClass(pill[0])}">${pill[1]}</span></div>
      <div class="state">${esc(stateText(x))}</div>
      <div class="ctl">${devButtons(x,blocked)}</div>
      ${blocked?`<p class="never">${esc(blocked)}</p>`:''}
      <label class="never"><input type="checkbox" data-veto="${esc(x.key)}" ${vetoed?'checked':''}>
        ${esc(NEVER[x.key]||`never use the ${label(x).toLowerCase()}`)}</label>
    </div>`}).join(''):'<div class="empty">No physical device evidence on the board</div>';
}

// Suggestions are derived from what the board already recorded — a veto that is
// costing energy, a plan move that measurably worked. No new storage.
// Every suggestion is derived from something the board already recorded. None
// of them invents a number, and each has exactly one thing to do about it.


// Two views, one clock. Temperature answers "what did the room do, and what
// would it have done untouched" — the gap between those lines is the whole
// value of the appliance, shown rather than claimed. Comfort answers "how did
// that feel", which is the same hour in the units a person actually lives in.
// Separate rather than stacked: four traces on one axis is a chart nobody
// reads on a phone.
let CHART_VIEW='temp';

function chartGeometry(s){
  const t0=s[0].at,t1=s[s.length-1].at,span=Math.max(1,t1-t0);
  return {t0,t1,span,X:t=>40+(t-t0)/span*960};
}

function chartYAxis(lo,hi,Y,unit){
  return [0,.25,.5,.75,1].map(f=>{const v=lo+(hi-lo)*f,y=Y(v);
    return `<line x1="40" y1="${y.toFixed(1)}" x2="1000" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.05)" stroke-width="1"/>`
      +`<text x="34" y="${(y+4).toFixed(1)}" text-anchor="end" fill="#77828F" font-size="11">${num(v,1)}${unit}</text>`}).join('');
}

function chartHours(s,geo){
  return [0,.25,.5,.75,1].map(f=>{const t=geo.t0+geo.span*f;
    return `<text x="${geo.X(t).toFixed(1)}" y="262" text-anchor="middle" fill="#77828F" font-size="11">${
      new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</text>`}).join('');
}

// The occupancy strip is on both views: a night of readings is unexplainable
// without knowing whether anybody was in the room.
function chartPresence(s,geo){
  let g='',run=null;
  s.forEach((p,i)=>{const home=p.people>0;
    if(home&&!run)run={a:p.at};
    if((!home||i===s.length-1)&&run){
      g+=`<rect x="${geo.X(run.a).toFixed(1)}" y="242" width="${Math.max(1,geo.X(p.at)-geo.X(run.a)).toFixed(1)}" height="8" rx="3" fill="rgba(50,215,75,.45)"/>`;
      run=null}});
  return g;
}

function renderTemperatureView(s,geo,d){
  const Y=c=>230-((c-LO)/(HI-LO))*200;
  const ac=d.ac||{},set=ac.setpoint_c;
  let g=chartYAxis(LO,HI,Y,'°');
  if(set!=null)g+=`<rect x="40" y="${Y(set+COMFORT_BAND_C).toFixed(1)}" width="960" height="${(Y(set-COMFORT_BAND_C)-Y(set+COMFORT_BAND_C)).toFixed(1)}"
      fill="rgba(50,215,75,.08)" stroke="rgba(50,215,75,.25)" stroke-dasharray="3 4"/>`;
  g+=chartPresence(s,geo);
  // Drawn first and underneath, because it is the reference the solid line is
  // read against rather than a second reading of equal standing.
  const without=s.filter(p=>p.without_cooling_c!=null);
  if(without.length>1)
    g+=`<polyline fill="none" stroke="#FF9F0A" stroke-width="2" stroke-dasharray="5 4" opacity=".85" points="${
      without.map(p=>`${geo.X(p.at).toFixed(1)},${Y(p.without_cooling_c).toFixed(1)}`).join(' ')}"/>`;
  g+=`<polyline fill="none" stroke="#F5F7FA" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" points="${
    s.map(p=>`${geo.X(p.at).toFixed(1)},${Y(p.indoor_c).toFixed(1)}`).join(' ')}"/>`;
  return g;
}

function renderComfortView(s,geo){
  const pts=s.filter(p=>p.pmv!=null);
  if(pts.length<2)return '<text x="520" y="130" text-anchor="middle" fill="#77828F" font-size="13">No comfort history yet</text>';
  // Fixed PMV rails, not autoscaled: the band edges are the whole point, and a
  // scale that moved with the data would hide how close to them the room ran.
  const lo=-1.5,hi=1.5,Y=v=>230-((Math.max(lo,Math.min(hi,v))-lo)/(hi-lo))*200;
  let g=chartYAxis(lo,hi,Y,'');
  g+=`<rect x="40" y="${Y(ISO_B).toFixed(1)}" width="960" height="${(Y(-ISO_B)-Y(ISO_B)).toFixed(1)}"
      fill="rgba(50,215,75,.08)" stroke="rgba(50,215,75,.25)" stroke-dasharray="3 4"/>`;
  g+=`<line x1="40" y1="${Y(0).toFixed(1)}" x2="1000" y2="${Y(0).toFixed(1)}" stroke="#77828F" stroke-width="1" stroke-dasharray="2 5"/>`;
  g+=chartPresence(s,geo);
  g+=`<polyline fill="none" stroke="#F5F7FA" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" points="${
    pts.map(p=>`${geo.X(p.at).toFixed(1)},${Y(p.pmv).toFixed(1)}`).join(' ')}"/>`;
  g+=`<text x="46" y="${(Y(ISO_B)-5).toFixed(1)}" fill="#77828F" font-size="10">too warm above here</text>`;
  g+=`<text x="46" y="${(Y(-ISO_B)+13).toFixed(1)}" fill="#77828F" font-size="10">too cool below here</text>`;
  return g;
}

const LEGEND={
  temp:[['#F5F7FA','room'],['#FF9F0A','without cooling'],
        ['rgba(50,215,75,.6)','someone home'],['rgba(50,215,75,.25)','comfort window']],
  comfort:[['#F5F7FA','how it felt'],['rgba(50,215,75,.6)','someone home'],
           ['rgba(50,215,75,.25)','comfortable range']],
};

// Timestamp-based, not index-based: the underlying series grows every 10 s
// refresh, so an index window would silently drift onto different data
// under a user's hands. A time window stays exactly where they left it.
let CHART_ZOOM=null;


function bindInlineChartDrag(svg,geo,full){
  const area=$('chartBrushArea'),brush=$('chartBrushRect');
  if(!area)return;
  let dragStart=null;
  const toSvgX=clientX=>{const rect=svg.getBoundingClientRect();
    const px=(clientX-rect.left)/rect.width*1040;return Math.max(40,Math.min(1000,px));};
  const toTime=svgX=>geo.t0+(svgX-40)/960*geo.span;
  area.onpointerdown=e=>{dragStart=toSvgX(e.clientX);brush.style.display='';brush.setAttribute('x',dragStart);brush.setAttribute('width',0);};
  area.onpointermove=e=>{if(dragStart==null)return;
    const x=toSvgX(e.clientX),lo=Math.min(dragStart,x),w=Math.abs(x-dragStart);
    brush.setAttribute('x',lo);brush.setAttribute('width',w);};
  area.onpointerup=e=>{
    const x=toSvgX(e.clientX);
    if(dragStart!=null&&Math.abs(x-dragStart)>10){
      const t0=toTime(Math.min(dragStart,x)),t1=toTime(Math.max(dragStart,x));
      CHART_ZOOM={t0,t1};renderChart(STATE);
    }
    dragStart=null;brush.style.display='none';};
  area.onpointerleave=()=>{dragStart=null;brush.style.display='none';};
}

function renderChart(d){
  const full=(d.history||[]).filter(x=>x.indoor_c!=null);
  const svg=$('chart');
  if(full.length<2){svg.innerHTML='<text x="520" y="130" text-anchor="middle" fill="#77828F" font-size="13">Not enough history yet</text>';return}
  const s=CHART_ZOOM?full.filter(p=>p.at>=CHART_ZOOM.t0&&p.at<=CHART_ZOOM.t1):full;
  if(s.length<2){CHART_ZOOM=null;return renderChart(d);}  // zoomed past the edge of what is left
  const geo=chartGeometry(s);
  const body=CHART_VIEW==='comfort'?renderComfortView(s,geo):renderTemperatureView(s,geo,d);
  svg.setAttribute('viewBox','0 0 1040 270');
  svg.innerHTML=body+chartHours(s,geo)
    +`<rect id="chartBrushArea" x="40" y="6" width="960" height="236" fill="transparent" style="cursor:crosshair"/>`
    +`<rect id="chartBrushRect" class="zoombrush" x="0" y="6" width="0" height="236" style="display:none"/>`;
  $('chartLegend').innerHTML=LEGEND[CHART_VIEW].map(
    ([c,t])=>`<span><i style="background:${c}"></i>${esc(t)}</span>`).join('')
    +(CHART_ZOOM?`<button id="chartZoomReset" class="expandbtn" style="margin-left:auto">Full range</button>`:'');
  const rb=$('chartZoomReset'); if(rb)rb.onclick=()=>{CHART_ZOOM=null;renderChart(STATE);};
  bindInlineChartDrag(svg,geo,full);
  svg.style.cursor='crosshair';
  svg.ondblclick=()=>$('expandDayChart')?.click();

  const temps=s.map(p=>p.indoor_c),
        without=s.filter(p=>p.without_cooling_c!=null).map(p=>p.without_cooling_c),
        pmvs=s.filter(p=>p.pmv!=null).map(p=>p.pmv),
        inBand=pmvs.filter(v=>Math.abs(v)<=ISO_B).length;
  const chips=CHART_VIEW==='comfort'
    ? [[pmvs.length?`${num(inBand/pmvs.length*100,0)}%`:'--','of today comfortable'],
       [`${(geo.span/3600).toFixed(1)} h`,'of history']]
    : [[`${num(Math.min(...temps),1)}–${num(Math.max(...temps),1)}°`,'range today'],
       // Only claimed when the two series genuinely differ; with nothing
       // running they coincide and there is no saving to report.
       ...(without.length&&Math.max(...without)-Math.max(...temps)>0.1
           ? [[`${num(Math.max(...without)-Math.max(...temps),1)}°`,'cooler at the peak']] : []),
       [`${(geo.span/3600).toFixed(1)} h`,'of history']];
  $('dayChips').innerHTML=chips.map(([b,t])=>`<span class="statchip"><b>${esc(b)}</b>${esc(t)}</span>`).join('');
}

function bindChartTabs(){
  document.querySelectorAll('.viewtabs button').forEach(b=>{
    b.onclick=()=>{
      CHART_VIEW=b.dataset.view;
      document.querySelectorAll('.viewtabs button').forEach(
        x=>x.setAttribute('aria-selected',String(x===b)));
      if(STATE)renderChart(STATE);
    };
  });
  const ex=$('expandDayChart');
  if(ex)ex.onclick=()=>{
    if(!STATE)return;
    const s=(STATE.history||[]).filter(x=>x.indoor_c!=null);
    if(CHART_VIEW==='comfort'){
      openZoomChart('Comfort — PMV over the day',
        s.filter(p=>p.pmv!=null).map(p=>({at:p.at,value:p.pmv})),' PMV');
    }else{
      openZoomChart('Room temperature — with and without cooling',
        s.map(p=>({at:p.at,value:p.indoor_c})),'°C');
    }
  };
}

function render(d){
  STATE=d;statebar(d);
  text('roomTag',new Date().toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'}));
  renderHero(d);renderPlan(d);renderRail(d);renderEnergy(d);
  renderActions(d);renderHandWork(d);
  renderDevices(d);renderChart(d);
  text('foot',`Board console · schema ${d.schema_version||'?'} · ${d.system&&d.system.database?num(d.system.database.size_bytes/1048576,1)+' MB stored':''}`);
}

// ── interactions ──
// Every command carries `confirm`. The tap already expressed the intent, so the
// page sends it with the tap rather than making a person confirm twice.
// Room has a "holds for" selector; Workbench has no such control and holds
// for the same default every other manual tap uses when nothing else is said.
function ttl(){const e=$('holdFor');if(!e)return 30;
  return e.value==='until_auto'?null:Number(e.value)}
document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.id==='setUp'||b.id==='setDown'||b.id==='feelWarm'||b.id==='feelCold'){
    const cur=Number($('setVal').textContent);if(isNaN(cur))return;
    const up=b.id==='setUp'||b.id==='feelCold';   // "I feel cold" means warmer
    const next=Math.max(16,Math.min(30,cur+(up?1:-1)));
    text('setVal',next);
    // ttl_min is NULL here on purpose, and deliberately NOT ttl(). The
    // `holdFor` selector governs manual DEVICE overrides — switching the AC on
    // by hand should lapse, or a forgotten tap latches an appliance forever.
    // A setpoint is not a device command: capability() itself calls it a
    // preference, and a preference that silently expires after 30 minutes is
    // how the dial appeared to revert on its own. It now ends only when a
    // person changes it.
    //
    // `revision` is the row version this page last rendered. The server 409s a
    // write whose basis is stale, so a second device holding an older read
    // cannot overwrite a newer setpoint; refresh() below then re-renders the
    // stored value and the toast carries the server's own sentence.
    const done=await post('/api/ac','ac-conditioning-control',
      {confirm:true,action:'setpoint',value:next,ttl_min:null,
       revision:((STATE&&STATE.ac&&STATE.ac.setpoint)||{}).revision,
       reason:b.id.startsWith('feel')?'occupant said how it feels':'setpoint tap'});
    if(done)toast(b.id.startsWith('feel')?`Noted — holding ${next}° from now`:`Setpoint ${next}°`,'good');
    refresh();return}
  if(b.dataset.unveto){
    if(await post('/api/preferences','preference-update',
        {name:`allow_${b.dataset.unveto}`,value:true}))
      toast('Back in the plan','good');
    refresh();return}
});
// The command is journalled at once but the room only reflects it after the
// control loop's next tick, so an immediate refresh returns the state from
// BEFORE the tap and the card visibly snaps back — the optimistic layer in
// _JS_COMMON covers the value itself, and this staggered re-read is what
// clears it back to a confirmed reading once the loop has actually caught up.
bindDeviceTaps(()=>{refresh();setTimeout(refresh,2500);setTimeout(refresh,6000)});
// The checkbox reads "never use this", so a tick is allow=false.
document.addEventListener('change',async e=>{
  const c=e.target.closest('input[data-veto]');if(!c)return;
  if(await post('/api/preferences','preference-update',
      {name:`allow_${c.dataset.veto}`,value:!c.checked}))
    toast(c.checked?'BreezeIQ will not use it':'Back in the plan','good');
  refresh()});

async function refresh(){
  try{const r=await fetch('/api/dashboard',{cache:'no-store'});render(applyOptimistic(await r.json()))}
  catch(e){statebar({data_state:'failed',error:'Board API unavailable'})}}
async function refreshCap(){
  try{const r=await fetch('/api/manual',{cache:'no-store'});const d=await r.json();CAP=d.capability;
    const p=$('capPill');p.textContent=CAP.available?'controls ready':CAP.detail;
    p.className=pillClass(CAP.available?'good':'blocked');
    if(STATE)renderDevices(STATE)}
  catch(e){CAP={available:false,detail:'control check failed'}}}
bindChartTabs();refresh();refreshCap();setInterval(refresh,10000);setInterval(refreshCap,30000);
</script>
</body></html>'''

# ── Workbench ───────────────────────────────────────────────────────────

# ── the log page ────────────────────────────────────────────────────────────
# The Room card answers "what did it just do" and the bench answers "what
# happened in the last thirty commands". Neither answers "what did it do on
# Tuesday afternoon", and the journal holds ten days of it. One day at a time,
# oldest first so a day reads forwards, and the same projection both dashboards
# use so the log cannot tell a third story about the same command.
_LOG_CSS = r'''
.logbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.logbar select{background:var(--inset);color:var(--text);border:1px solid var(--line);
  border-radius:8px;padding:7px 10px;font:inherit;font-size:13px}
.logrow{display:grid;grid-template-columns:74px 1fr 120px 130px;gap:12px;
  align-items:baseline;padding:9px 12px;background:var(--surface2);
  border:1px solid var(--line);border-left:3px solid var(--line);border-radius:6px}
.logrow.done{border-left-color:var(--good)}
.logrow.warn{border-left-color:var(--warn)}
.logrow.bad{border-left-color:var(--bad)}
.logrow time{color:var(--faint);font-size:11.5px;font-variant-numeric:tabular-nums}
.logrow .who{color:var(--muted);font-size:12px}
.logrow .out{text-align:right}
.logrow em{display:block;font-style:normal;color:var(--faint);font-size:11.5px;margin-top:3px}
.loglist{display:flex;flex-direction:column;gap:6px}
.logcount{color:var(--muted);font-size:12.5px}
@media(max-width:720px){.logrow{grid-template-columns:64px 1fr;}
  .logrow .who,.logrow .out{grid-column:2;text-align:left}}
'''

LOG_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BreezeIQ — Log</title>
<style>''' + _CSS + _LOG_CSS + r'''</style></head>
<body>
<header class="top">
  <div class="brand">BreezeIQ</div>
  <nav class="seg"><a href="/">Room</a><a href="/workbench">Workbench</a><a href="/log" aria-current="page">Log</a></nav>
</header>
<main class="wrap">
  <div class="card">
    <div class="eyebrow">Every command, by day</div>
    <div class="logbar">
      <select id="dayPick" aria-label="Which day"></select>
      <span class="logcount" id="dayCount">loading</span>
    </div>
    <div class="loglist" id="logRows"><div class="empty">Loading</div></div>
  </div>
</main>
<script>''' + _JS_COMMON + r'''
function stamp(at){if(at==null)return'--';
  const d=new Date(at*1000);
  return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}

async function loadDays(){
  let j;try{j=await (await fetch('/api/log/days')).json()}catch(e){j={days:[]}}
  const days=(j.days||[]).filter(x=>x.day);
  const sel=$('dayPick');
  if(!days.length){sel.innerHTML='<option>no commands yet</option>';
    $('dayCount').textContent='the journal is empty';
    $('logRows').innerHTML='<div class="empty">Nothing has been commanded yet</div>';return}
  sel.innerHTML=days.map(x=>`<option value="${esc(x.day)}">${esc(x.day)} &middot; ${x.commands}</option>`).join('');
  sel.onchange=()=>loadDay(sel.value);
  loadDay(days[0].day);
}

async function loadDay(day){
  $('logRows').innerHTML='<div class="empty">Loading '+esc(day)+'</div>';
  let j;try{j=await (await fetch('/api/log?day='+encodeURIComponent(day))).json()}
  catch(e){j={commands:[],error:'could not load'}}
  const rows=j.commands||[];
  $('dayCount').textContent=j.error?j.error
    :`${rows.length} command${rows.length===1?'':'s'}`+(j.truncated?' (truncated)':'');
  $('logRows').innerHTML=rows.length?rows.map(a=>{
    const extra=[a.outcome_detail,a.detail,
      a.acknowledged==null?'no acknowledgement recorded'
        :`acknowledged: ${a.acknowledged?'yes':'no'}`].filter(Boolean).join(' \u00b7 ');
    return `<div class="logrow ${ACT_STRIPE[a.outcome]||''}">
      <time>${esc(stamp(a.at))}</time>
      <span><b>${esc(actionName(a))} ${esc(a.what)}</b><em>${esc(a.why||'reason not recorded')}${extra?' \u00b7 '+esc(extra):''}</em></span>
      <span class="who">${esc(a.actor||'actor not recorded')}</span>
      <span class="out"><span class="${pillClass(OUTCOME_PILL[a.outcome]||'dim')}">${esc(a.outcome)}</span></span>
    </div>`}).join(''):'<div class="empty">No commands on this day</div>';
}
loadDays();
</script>
</body></html>'''

BENCH_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BreezeIQ — Workbench</title>
<style>''' + _CSS + _BENCH_CSS + r'''</style></head>
<body>
<header>
  <span class="wordmark">Breeze<em>IQ</em></span>
  <span class="roomtag">Workbench · click a part</span>
  <span class="spacer"></span>
  <nav class="seg"><a href="/">Room</a><a href="/workbench" aria-current="page">Workbench</a><a href="/log">Log</a></nav>
</header>
<div class="statebar" id="statebar" data-state="failed">
  <strong id="stateTitle">Connecting</strong><span class="sub" id="stateDetail"></span>
</div>

<main>
<div class="glance num" id="glance"></div>

<div class="engwrap">
  <div class="card" style="padding:10px 12px">
  <div class="boardwrap">
  <svg class="board" viewBox="0 0 1110 660" role="img" aria-label="Wiring bench. Click any part for detail.">
    <path id="w-dht_in" d="M248,142 C290,142 300,168 336,168" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-dht_out" d="M248,202 C290,202 300,216 336,216" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-pir" d="M248,262 C290,262 300,264 336,264" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-ldr" d="M248,322 C290,322 300,312 336,312" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-mq" d="M248,382 C290,382 300,360 336,360" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-bh" d="M248,478 C300,478 300,430 336,424" stroke="#77828F" stroke-width="2" fill="none"/>
    <path id="w-cam" d="M862,150 C820,150 812,170 778,170" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-motor" d="M862,220 C820,220 812,240 774,240" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-blinds" d="M862,290 C820,290 812,288 774,288" stroke="#77828F" stroke-width="2.5" fill="none"/>
    <path id="w-fan" d="M862,360 C830,360 790,200 745,166" stroke="#5AC8FA" stroke-width="2" stroke-dasharray="3 6" fill="none"/>
    <path id="w-ac" d="M862,430 C826,430 786,210 745,170" stroke="#5AC8FA" stroke-width="2" stroke-dasharray="3 6" fill="none"/>
    <path id="w-lamp" d="M862,500 C822,500 782,220 745,174" stroke="#5AC8FA" stroke-width="2" stroke-dasharray="3 6" fill="none"/>
    <path id="w-radar" d="M862,570 C800,570 800,470 774,452" stroke="#77828F" stroke-width="2" fill="none"/>

    <g class="hot" data-id="mpu" tabindex="0" role="button" aria-label="Linux processor">
      <rect x="336" y="96" width="438" height="470" rx="18" fill="var(--pcb)" stroke="#2e3d4e" stroke-width="1.5"/>
      <circle cx="358" cy="118" r="7" fill="var(--bg)" stroke="#2e3d4e"/><circle cx="752" cy="118" r="7" fill="var(--bg)" stroke="#2e3d4e"/>
      <circle cx="358" cy="544" r="7" fill="var(--bg)" stroke="#2e3d4e"/><circle cx="752" cy="544" r="7" fill="var(--bg)" stroke="#2e3d4e"/>
      <text x="380" y="126" fill="#77828F" font-size="13" letter-spacing="2">ARDUINO UNO Q</text>
      <line x1="352" y1="150" x2="352" y2="440" stroke="var(--gold)" stroke-width="8" stroke-dasharray="6 7" opacity=".45"/>
      <line x1="758" y1="150" x2="758" y2="470" stroke="var(--gold)" stroke-width="8" stroke-dasharray="6 7" opacity=".45"/>
      <rect x="762" y="156" width="18" height="28" rx="6" fill="#223142" stroke="#33465a"/>
      <circle cx="741" cy="170" r="4" fill="#5AC8FA"/>
      <path d="M733,162 a11,11 0 0 1 16,0 M728,155 a18,18 0 0 1 26,0" stroke="#5AC8FA" stroke-width="1.6" fill="none" opacity=".7"/>
      <text x="748" y="198" fill="#77828F" font-size="10" text-anchor="end">Wi-Fi</text>
      <rect x="416" y="150" width="200" height="150" rx="10" fill="var(--pcb2)" stroke="#33465a"/>
      <text x="424" y="166" fill="#77828F" font-size="9">U1</text>
      <text x="516" y="205" fill="#F5F7FA" font-size="14" font-weight="600" text-anchor="middle">Qualcomm QRB2210</text>
      <text x="516" y="226" fill="#9CA7B4" font-size="11.5" text-anchor="middle">Linux · brain + planner</text>
      <text id="b-mpu" x="516" y="250" fill="#9CA7B4" font-size="11.5" text-anchor="middle">--</text>
      <text x="516" y="284" fill="#77828F" font-size="10.5" text-anchor="middle">click for CPU · RAM · storage</text>
    </g>
    <g class="hot" data-id="mcu" tabindex="0" role="button" aria-label="Real-time microcontroller">
      <rect x="416" y="380" width="200" height="120" rx="10" fill="var(--pcb2)" stroke="#33465a"/>
      <text x="424" y="396" fill="#77828F" font-size="9">U2</text>
      <text x="516" y="424" fill="#F5F7FA" font-size="14" font-weight="600" text-anchor="middle">STM32U585</text>
      <text x="516" y="445" fill="#9CA7B4" font-size="11.5" text-anchor="middle">real-time · 1 Hz sampling</text>
      <text id="b-mcu" x="516" y="464" fill="#9CA7B4" font-size="11.5" text-anchor="middle">--</text>
    </g>
    <path d="M516,300 L516,380" stroke="#33465a" stroke-width="2"/>
    <text x="524" y="345" fill="#77828F" font-size="10">RPC bridge</text>
    <rect x="640" y="380" width="110" height="70" rx="7" fill="#22384a"/>
    <g fill="#5AC8FA" opacity=".8">
      <circle cx="660" cy="398" r="3"/><circle cx="676" cy="398" r="3"/><circle cx="708" cy="398" r="3"/>
      <circle cx="668" cy="414" r="3"/><circle cx="692" cy="414" r="3"/><circle cx="724" cy="414" r="3"/>
      <circle cx="660" cy="430" r="3"/><circle cx="700" cy="430" r="3"/></g>
    <text x="695" y="466" fill="#77828F" font-size="10" text-anchor="middle">LED matrix</text>
    <g fill="#9CA7B4" font-size="11" font-family="ui-monospace,Menlo,monospace">
      <text x="360" y="172">D7</text><text x="360" y="220">D4</text><text x="360" y="268">D8</text>
      <text x="360" y="316">A0</text><text x="360" y="364">A1</text><text x="360" y="428">I2C</text>
      <text x="750" y="174" text-anchor="end">USB-C</text><text x="750" y="244" text-anchor="end">D9</text>
      <text x="750" y="292" text-anchor="end">D10</text><text x="750" y="456" text-anchor="end">UART</text></g>
    <g id="parts"></g>
    <text x="52" y="620" fill="#77828F" font-size="11.5">solid = wired pin · dashed blue = Wi-Fi · dashed grey = not fitted yet · dot = live health</text>
  </svg>
  </div>
  </div>
  <div class="panel"><div class="card" id="detail"></div></div>
</div>

<div class="row cols-insight" style="margin-top:14px">
  <div class="card">
    <div class="eyebrow">Planner — why it stayed silent</div>
    <dl class="kv num" id="planKv"></dl>
    <div class="declist" id="planRows"></div>
  </div>
  <div class="card">
    <div class="eyebrow">Storage</div>
    <div class="bars num" id="storeBars"></div>
    <dl class="kv num" id="storeKv"></dl>
  </div>
</div>

<div class="row" style="margin-top:14px"><div class="card">
  <div class="eyebrow">Commands sent — what · why · actor · outcome</div>
  <div class="actlist num" id="actionRows"></div>
</div></div>
</main>
<div class="toast" id="toast"></div>
<footer id="foot"></footer>

<script>''' + _JS_COMMON + r'''
let STATE=null,SEL='mpu',CAP=null;
// Physical layout: where each part sits on the bench and what it is wired to.
// Positions are fixed because the wiring is; only values and health change.
const PARTS=[
 {id:'dht_in', x:40,y:120, ref:'S1', name:'DHT22 · indoor',  pin:'D7'},
 {id:'dht_out',x:40,y:180, ref:'S2', name:'DHT22 · outdoor', pin:'D4'},
 {id:'pir',    x:40,y:240, ref:'S3', name:'PIR motion',      pin:'D8'},
 {id:'ldr',    x:40,y:300, ref:'S4', name:'LDR · light',     pin:'A0'},
 {id:'mq',     x:40,y:360, ref:'S5', name:'MQ-135 · air',    pin:'A1'},
 {id:'bh',     x:40,y:456, ref:'L1', name:'BH1750 · lux',     pin:'SDA/SCL'},
 {id:'cam',    x:862,y:128,ref:'C1', name:'USB camera',      pin:'USB-C'},
 {id:'blinds', x:862,y:268,ref:'M1', name:'Blinds motor',    pin:'D13/12/11~'},
 {id:'fan',    x:862,y:338,ref:'W1', name:'Ceiling fan',     pin:'Wi-Fi'},
 {id:'ac',     x:862,y:408,ref:'W2', name:'Air conditioner', pin:'Wi-Fi'},
 {id:'lamp',   x:862,y:478,ref:'W3', name:'Tubelight',       pin:'Wi-Fi'},
 {id:'radar',  x:862,y:548,ref:'R1', name:'LD2410C radar',   pin:'D2'},
];
const HEALTH={good:'#32D74B',warn:'#FF9F0A',bad:'#FF453A',dim:'#5A6572'};

function drawParts(){
  $('parts').innerHTML=PARTS.map(p=>`
    <g class="hot" data-id="${p.id}" tabindex="0" role="button" aria-label="${esc(p.name)}">
      <rect id="box-${p.id}" class="chipbox" x="${p.x}" y="${p.y}" width="208" height="44" rx="10"/>
      <circle id="dot-${p.id}" cx="${p.x+20}" cy="${p.y+22}" r="4" fill="#77828F"/>
      <text x="${p.x+200}" y="${p.y+13}" fill="#77828F" font-size="9" text-anchor="end">${p.ref}</text>
      <text x="${p.x+34}" y="${p.y+18}" fill="#F5F7FA" font-size="13" font-weight="600">${esc(p.name)}</text>
      <text id="val-${p.id}" x="${p.x+34}" y="${p.y+34}" fill="#9CA7B4" font-size="11">--</text>
    </g>`).join('');
  document.querySelectorAll('.hot').forEach(g=>{
    g.addEventListener('click',()=>{SEL=g.dataset.id;paint()});
    g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();SEL=g.dataset.id;paint()}})});
}

// One descriptor per part: the live line on the bench, and the five answers the
// panel gives. Everything is read from the payload the board already publishes.
function describe(d){
  const c=d.comfort||{},o=d.occupancy||{},raw=d.debug_evidence||{},t=d.digital_twin||{},
        sys=d.system||{},host=sys.host||{},db=(sys.database||{}),link=d.camera_link||{},
        dev=k=>(d.devices||[]).find(x=>x.key===k)||{},
        pref=d.preferences||{};
  const ldrBad=c.light_state==null||c.light_state==='unavailable';
  const camOff=link.present===false;
  return {
   mpu:{h:host.constrained_used_pct>=95?'warn':'good',
     line:`load ${num(host.load_1m,2)} · ${num(db.size_bytes/1048576,1)} MB`,
     name:'Qualcomm QRB2210 — Linux side',pin:'U1',big:`load ${num(host.load_1m,2)}`,
     kv:[['Memory used',host.memory_total_kb
            ?`${num((host.memory_total_kb-host.memory_available_kb)/1024,0)} of ${num(host.memory_total_kb/1024,0)} MB`:'--'],
         ['Data partition',host.data_total_bytes
            ?`${num(host.data_free_bytes/1073741824,1)} GB free of ${num(host.data_total_bytes/1073741824,1)}`:'--'],
         [`Root (${esc(host.constrained_mount||'/')})`,host.constrained_used_pct!=null
            ?`${num(host.constrained_used_pct,1)}% used`:'--'],
         ['Uptime',host.linux_uptime_s?`${num(host.linux_uptime_s/3600,1)} h`:'--'],
         ['Database',`${num(db.size_bytes/1048576,1)} MB · ${db.rows||0} ticks`],
         ['Sample age',age((d.sample||{}).age_s)]],
     diag:host.constrained_used_pct>=95
       ?['warn',`The root filesystem is ${num(host.constrained_used_pct,1)}% full. Telemetry lives on the data partition and has room, but a full root will stop the container from starting.`]
       :['good','Brain, planner, camera inference, database and both consoles run on this side. If this page goes stale, look here first.'],
     store:'owns telemetry.sqlite3 — every table'},
   mcu:(()=>{
     // Live values off the wire, not asserted strings: the failsafe flag and
     // episode counter come from the frame itself, so a real silence-park
     // during a bench session is visible instead of invisible.
     const fsNow=c.failsafe_active===true, fsN=c.failsafe_episodes;
     return {h:fsNow?'warn':'good',
       line:fsNow?'FAILSAFE ENGAGED — parked':`1 Hz · failsafe armed${fsN?` · fired ${fsN}×`:''}`,
       name:'STM32U585 — real-time side',pin:'U2',
       big:c.fw_build!=null?`build ${c.fw_build}`:'5 channels',
       kv:[['Sampling','1 Hz, fixed — never blocked by Linux'],
           ['Drives','L298 motor bridge · LED matrix'],
           ['Failsafe now',c.failsafe_active==null?'--':(fsNow?'ENGAGED — blinds parked open':'armed, quiet')],
           ['Failsafe episodes since boot',fsN==null?'--':String(fsN)],
           ['Firmware build',c.fw_build==null?'--':String(c.fw_build)],
           ['Headroom','160 MHz M33, mostly idle']],
       diag:fsNow
         ?['warn','The 10 s Bridge-silence failsafe is engaged right now: the MCU parked the blinds open on its own. It re-arms automatically once calls resume.']
         :['good','Deterministic domain: sampling and safety keep running even if the Linux side dies. The failsafe row above is live off the wire, not an assumption.'],
       store:'feeds tick.* raw channels over the bridge'};
   })(),
   dht_in:{h:'good',line:`${num(c.indoor_c,1)}° · ${num(c.indoor_rh,0)}% RH`,
     name:'DHT22 · indoor',pin:'D7 · S1',big:`${temp(raw.indoor_c!=null?raw.indoor_c:c.indoor_c)} · ${num(c.indoor_rh,0)} %RH`,
     kv:[['Sensor, uncorrected',temp(raw.indoor_raw_c)],
         ['Calibration offset',raw.indoor_raw_c!=null&&raw.indoor_c!=null?`${num(raw.indoor_c-raw.indoor_raw_c,2)} °C`:'--'],
         ['Sensor, calibrated',temp(raw.indoor_c)],
         ['Room value used',temp(c.indoor_c)],
         ['Humidity',c.indoor_rh==null?'--':`${num(c.indoor_rh,1)} %RH`],
         ['Age',age((d.sample||{}).age_s)]],
     diag:['good','Three figures, and the gaps between them are the two corrections. The offset is applied on the board against a trusted reference; the room value then adds the cooling response, so it converges on the calibrated reading whenever cooling is off and settled.'],
     store:'writes tick.indoor_c · indoor_raw_c · indoor_rh every tick'},
   dht_out:{h:'good',line:`${num(c.outdoor_c,1)}° · ${num(c.outdoor_rh,0)}% RH`,
     name:'DHT22 · outdoor',pin:'D4 · S2',big:`${temp(c.outdoor_c)} · ${num(c.outdoor_rh,0)} %RH`,
     kv:[['Sensor, uncorrected',temp(raw.outdoor_raw_c)],
         ['Calibration offset',raw.outdoor_raw_c!=null&&raw.outdoor_c!=null?`${num(raw.outdoor_c-raw.outdoor_raw_c,2)} °C`:'--'],
         ['Sensor, calibrated',temp(raw.outdoor_c)],
         ['Humidity',c.outdoor_rh==null?'--':`${num(c.outdoor_rh,1)} %RH`],
         ['Delta to indoor',c.outdoor_c!=null&&c.indoor_c!=null?`${num(c.outdoor_c-c.indoor_c,1)} °C`:'--'],
         ['Role','drift target when nothing runs']],
     diag:['good','The projection builds its no-cooling drift line from this, so its offset matters as much as the indoor one.'],
     store:'writes tick.outdoor_c · outdoor_raw_c · outdoor_rh'},
   pir:{h:'good',line:o.pir_motion?'motion now':'still',name:'PIR motion',pin:'D8 · S3',
     big:o.pir_motion?'motion':'still',
     kv:[['Level now',o.pir_motion?'HIGH':'LOW'],
         ['Last motion',o.pir_last_motion_at?age(Date.now()/1000-o.pir_last_motion_at):'--'],
         ['Role','presence floor + wake trigger'],['Fused state',o.occupant||'--']],
     diag:['good','Quiet-but-present is normal while somebody sits still — the camera vote covers it.'],
     store:'writes tick.pir_motion · pir_last_motion_at'},
   ldr:{h:ldrBad?'bad':'good',line:`raw ${raw.light_raw==null?'--':raw.light_raw} · ${c.light_state||'unavailable'}`,
     name:'LDR · light',pin:'A0 · 10k divider · S4',big:`${raw.light_raw==null?'--':raw.light_raw} / 4095`,
     kv:[['Raw counts',raw.light_raw==null?'--':`${raw.light_raw} / 4095`],
         ['Calibration span',c.ldr_min!=null&&c.ldr_max!=null?`${c.ldr_min} dark → ${c.ldr_max} sun (${c.ldr_max-c.ldr_min>500?'healthy':'under the 500 floor'})`:'--'],
         ['Solar index',raw.solar_index==null?'excluded':num(raw.solar_index,3)],
         ['Provenance',(c.light_provenance||[]).join(' + ')||'--'],
         ['Fused state',c.light_state||'unavailable'],['Confidence',c.light_confidence||'--']],
     diag:ldrBad
       ?['bad','Calibration guard rejected this channel: the span across the window is under the 500-count floor, which is a broken path rather than a dark room. Check the divider joint and the A0 header seat. Nothing downstream is polluted — the brain already refuses it.']
       :['good','Span is healthy and the fused light state is being trusted.'],
     store:'writes tick.light_raw · solar_index'},
   mq:{h:'good',line:`${raw.air_raw==null?'--':raw.air_raw} / 4095`,name:'MQ-135 · air',pin:'A1 · S5',
     big:`${raw.air_raw==null?'--':raw.air_raw} / 4095`,
     kv:[['Raw counts',raw.air_raw==null?'--':`${raw.air_raw} / 4095`],
         ['As voltage',raw.air_raw==null?'--':`${num(raw.air_raw/4095*3.3,2)} V at the pin`],
         ['Claim','relative trend only — no ppm'],['Use','a sustained rise favours fresh air']],
     diag:['good','Reported as a trend, never as a calibrated concentration.'],
     store:'writes tick.air_raw'},
   bh:bhDesc(c),
   cam:{h:camOff?'bad':(o.camera_fault?'warn':'good'),
     line:camOff?'off the bus':`${o.count==null?'--':o.count} person · ${num((o.confidence||0)*100,0)}%`,
     name:'USB camera · person count',pin:'USB-C · C1',
     big:camOff?'off the bus':`${o.count==null?'--':o.count} · ${num((o.confidence||0)*100,0)}% conf`,
     kv:[['USB bus',link.present?'attached':'not enumerated'],
         ['Downstream devices',(link.downstream||[]).length
            ?`${(link.downstream||[]).length} below the root hubs`
            :'none — only the controllers themselves'],
         ['Device number',link.devnum||'--'],['Video nodes',(link.nodes||[]).join(', ')||'none'],
         ['Last detection',o.camera_last_valid_at?age(Date.now()/1000-o.camera_last_valid_at):'never'],
         ['Health',o.camera_health||'--'],
         // Served by us now, from the YOLO counter's own frames (same process
         // as this console), not the App Lab Brick's :4912 server. On-demand
         // JPEG, never stored — a live view, not a recording.
         ['Live preview',camOff?'none — no camera on the bus':'live · /api/camera/preview.mjpeg (on-device)'],
         ['Privacy','frames counted, then discarded']],
     // NAME THE EVIDENCE, DO NOT GUESS THE CAUSE. This used to assert one story
     // unconditionally — a bus-powered camera browning out on a shared hub,
     // "a climbing number is the fault, counted". Nothing counted it, and the
     // number it blamed is null in exactly the state that printed the message.
     // Twice it sent a reader to re-cable a hub that was not the problem.
     //
     // The replacement branched on the Type-C partner and the root hub, and on
     // the real board — camera attached, counting, healthy — there is NO Type-C
     // partner and the root hubs are always present, so it collapsed to the
     // first arm and printed "re-seat the USB-C cable" for every fault there is.
     // Same wrong advice as the version it replaced, reached more elaborately.
     // Only branch on a fact you have watched CHANGE. See dashboard._camera_link.
     diag:camOff
       ?(!(link.downstream||[]).length
          ?['bad','Nothing is enumerated below the root hubs — not the hub, not the camera, nothing. That is the cable, the socket, or a hub with no power of its own, and it is below any software.']
          :['bad',`The bus has ${(link.downstream||[]).length} devices on it but no capture node. The cable is carrying everything else, so the camera itself, or its own lead into the hub, is the part to check.`])
       :[o.camera_fault?'warn':'good',o.camera_fault||'Counting normally. Occupancy also survives dropouts on PIR and the presence hold.'],
     store:'writes tick.people · people_confidence · camera_light',
     // Offered only when a camera is on the bus. The stream is served by this
     // console itself (see dashboard._camera_stream), so the link is same-origin
     // and port-agnostic — no hardcoded :4912 that answers "connection refused"
     // whenever the Brick is not the one serving frames.
     act:camOff?null:[['Open live view',`/api/camera/preview.mjpeg`]]},
   blinds:(()=>{
     // A DC motor is commanded a DIRECTION for a TIME and then coasts, and it
     // has no feedback wire — so "position" on this card is what was last
     // commanded, never what was measured, and real drift accumulates.
     const dir=c.motor_dir, spd=c.motor_speed;
     const moving=dir!=null&&dir!==0;
     const kv=[['Position',isOn(dev('blinds'))?'OPEN — letting sun in':'SHUT — shading the glass'],
         ['Motor','RS555SF/3162 DC · 12 V-rated'],
         ['Driver','L298 · bare IC, not an N module'],
         ['IA1 · direction','D13'],
         ['IA2 · direction','D12'],
         ['EA · enable + speed','D11~ (PWM)'],
         ['Supply','rail C 9 V · 1000 µF at the motor']];
     // This is the control loop's last tick, not a live pin read — the loop
     // ticks every ~30s, so a bench run under 30s usually starts and ends
     // between samples and this will read "coasting" while the motor is
     // genuinely turning. The ACK printed on the bench panel after a send is
     // the real per-command proof; this row is only good for state that
     // outlives a tick (a stuck Hold, a runaway drive).
     if(dir!=null) kv.push(['Motor now (last tick, up to 30s old)',
       moving?(dir>0?'driving open':'driving shut'):'coasting']);
     if(moving&&c.motor_left_s!=null) kv.push(['Run window left (device truth)',
       `${num(c.motor_left_s,1)} s`]);
     if(spd!=null) kv.push(['Enable (PWM)',spd===0?'0 — bridge off':`${num(spd/255*100,0)}% of full`]);
     if(c.reading&&c.reading.age_s!=null) kv.push(['Sample age',age(c.reading.age_s)]);
     return {h:'good',line:moving?(dir>0?'driving open':'driving shut'):(isOn(dev('blinds'))?'shut':'open'),
       name:'Blinds — RS555SF/3162 via L298',pin:'D13 · D12 · D11~ · M1',
       big:moving?(dir>0?'opening':'closing'):(isOn(dev('blinds'))?'shut':'open'),kv:kv,
       diag:['good','Cardstock slats genuinely shade the sensor, so the solar term this earns is real. Position is the last command, not a measurement — a DC motor has no feedback wire, so travel time is what sets the endpoint.'],
       store:'writes act_command · act_reported_state',
       // No ladder Open/Shut row here on purpose: blinds is not in the manual
       // capability list, so those buttons render disabled with an "automatic
       // only" note directly above a panel that DOES work — which reads as
       // "controls are dead". The bench panel is the manual control.
       bench:motorBench()};
   })(),
   fan:(()=>{
     // `pref.veto_fan` was read here and no such key is published — the row
     // said "no" whatever the occupant had asked for. The payload spells it
     // `allow_fan`, and an absent row means allowed.
     const vetoed=pref.allow_fan===false, q=d.fan_quota||{},
           spent=QUOTA_BLOCKED[q.state];
     return {h:spent?'warn':vetoed?'warn':'good',line:stateText(dev('fan')),
       name:'Ceiling fan',pin:'Wi-Fi · W1',big:stateText(dev('fan')),
       kv:[['Vetoed',vetoed?'yes — standing preference':'no'],
           ['Range','8–32 W by speed'],
           ['Vendor calls today',q.calls_today==null?'--':`${q.calls_today} of ${q.budget}`],
           ['Calls left today',q.remaining==null?'--':String(q.remaining)],
           ['Budget state',q.state||'--'],
           ['Presence','free UDP LAN beacon; commands cost quota']],
       // Quota first: a spent budget is why the card reads unavailable, and
       // reporting the veto instead sends a reader to the wrong control.
       diag:spent?['warn',q.message||'The fan cannot be commanded right now.']
         :vetoed?['warn','Vetoed: the plan routes around it and may reach for cooling instead.']
         :['good','Available to the plan as the cheap rung below cooling. Presence is a free LAN beacon; every command spends one of the day’s vendor calls.'],
       store:'writes act_command · energy_sample (measured)',
       ctl:devButtons(dev('fan'),whyBlocked(dev('fan'),pref)),
       blocked:whyBlocked(dev('fan'),pref)};
   })(),
   ac:{h:'good',line:isOn(dev('ac'))?'running':'off',name:'Air conditioner',pin:'Wi-Fi · W2',
     big:isOn(dev('ac'))?'running':'off',
     kv:[['Setpoint',temp(t.setpoint_c)],['Room value',temp(t.modeled_c)],
         ['Raw sensor',temp(t.sensed_indoor_c)],['Reduction',t.reduction_c==null?'--':`${num(t.reduction_c,1)} °C`],
         ['Status',t.status||'--'],['Compressor guard','min-off 180 s, enforced below the brain']],
     calc:twinCalc(t.explanation),
     diag:['good','Switching and readback are real and logged separately. The room response to it is computed from a named appliance profile — the working below shows every input it used and the one step it applied, so the number above can be checked rather than believed.'],
     store:'writes response tables · act_command',
     ctl:devButtons(dev('ac'),whyBlocked(dev('ac'),pref)),
     blocked:whyBlocked(dev('ac'),pref)},
   lamp:{h:'good',line:stateText(dev('light')),
     name:'Tubelight',pin:'Wi-Fi · W3',big:isOn(dev('light'))?'on':'off',
     kv:[['Rule','dark + someone here + not asleep'],['Night window','23:00–07:00 stays off'],
         ['Debounce','2 min of agreement before switching on']],
     diag:['good','The debounce is what stopped a single spurious dark reading flipping this at 3 a.m.'],
     store:'writes act_command',
     ctl:devButtons(dev('light'),whyBlocked(dev('light'),pref)),
     blocked:whyBlocked(dev('light'),pref)},
   radar:radarDesc(c,o,d.history||[]),
  };
}

// R5. Nothing is wired to the AC switch, so every temperature on that card is
// arithmetic and has to be auditable on sight. Four sections in the order the
// tick ran them, each headed by what its values ARE rather than by what they
// describe: `measured` is only what a sensor reported, `modelled` only what the
// model computed, and the two in between are the inputs and the single step.
// A tick that did not advance the model prints why, never a zero.
const rate=v=>v==null?'--':`${num(v,2)} °C/h`;
function twinCalc(x){
  if(!x)return '';
  const sec=(cls,head,rows)=>`<div class="calcsec ${cls}"><div class="calchead">${esc(head)}</div>${
    rows.map(([k,v])=>`<div class="calcrow"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join('')}</div>`;
  const m=x.measured||{},i=x.inputs||{},s=x.step,o=x.modelled||{};
  let html=sec('measured','Measured — read off a sensor',[
    ['Indoor thermometer',temp(m.indoor_c)],
    ['Indoor humidity',m.indoor_rh==null?'--':`${num(m.indoor_rh,1)} %RH`],
    ['Outdoor thermometer',temp(m.outdoor_c)]]);
  html+=x.inputs?sec('','Inputs it was given',[
    ['Switch',i.power?'on':'off'],
    ['Setpoint asked for',temp(i.setpoint_c)],
    ['Control',i.mode||'--'],
    // Name, tonnage and kW together: "1.5T" is marketing tonnage and 5.0 kW
    // is 1.42 refrigeration tons, so showing one without the other invites
    // exactly the wrong arithmetic.
    ['Appliance',i.ac_label||i.ac_model||'--'],
    ['Cooling target',temp(i.setpoint_c)],
    ['Step length',i.dt_s==null?'--':`${num(i.dt_s,0)} s`],
    ['Cooling at full output',rate(i.cooling_rate_c_h)],
    ['Coast rate, switch open',rate(i.coast_rate_c_h)],
    ['Envelope conductance',i.ua_w_per_c==null?'--':`${num(i.ua_w_per_c,1)} W/°C`],
    ['Reachable below ambient',i.reachable_depth_c==null?'--':`${num(i.reachable_depth_c,1)} °C`]])
    :sec('','Inputs it was given',[['Configuration','unavailable']]);
  html+=s?sec('','The one step it applied',[
    ['Started from',temp(s.from_c)],
    ['Envelope drift, toward the thermometer',rate(s.drift_c_h)],
    ['Appliance cooling, toward the setpoint',rate(s.cooling_c_h)],
    ['Net rate = drift − cooling',rate(s.net_rate_c_h)],
    ['Applied over this step',s.applied_c==null?'--':`${num(s.applied_c,3)} °C`],
    ['Coldest this step could reach',temp(s.floor_c)]])
    :sec('','The one step it applied',[['Step','unavailable — no step this tick']]);
  html+=sec('modelled','Modelled — computed, not measured',[
    ['Room value',temp(o.room_c)],
    ['Room humidity',o.room_rh==null?'--':`${num(o.room_rh,1)} %RH`],
    ['Status',o.status||'--'],
    ['Compressor output',o.cooling_effect==null?'--':`${num(o.cooling_effect*100,0)}% of rated`],
    ['Estimated draw',o.estimated_watts==null?'--':`${num(o.estimated_watts,0)} W`],
    ['This interval',o.interval_wh==null?'--':wh(o.interval_wh)]]);
  return `<div class="calc">${html}${
    x.detail?`<div class="calcnote">${esc(x.detail)}</div>`:''}</div>`;
}

// Both parts below report their own presence, which the analogue channels
// cannot: an I2C device either ACKs or it does not, and a digital OUT is a
// level. So neither descriptor guesses — "not fitted" is a reading here, not
// an assumption baked into the page.

// The pull-up state is read on the MCU before I2C claims the pins, so it
// separates the two failures that otherwise look identical: a part that is
// unpowered, and a part that is powered but at an address nobody probed.
function bhDesc(c){
  const lux=c.lux, addr=c.lux_addr, bus=c.i2c_devices,
        sda=c.sda_pullup, scl=c.scl_pullup,
        pulledUp=sda===1||sda===true, sclUp=scl===1||scl===true,
        dead=sda!=null&&!pulledUp&&!sclUp;
  const base={name:'BH1750 · lux',pin:'SDA/SCL · 0x23 · L1',
    store:'writes tick.lux · lux_addr · i2c_devices'};
  if(lux!=null){
    const busNames=['Wire — i2c2, PB10/PB11 (D21/D20)','Wire1 — i2c4 (Qwiic)','Wire2 — i2c3, the SCL/SDA pads'];
    const kv=[['Address',addr?`0x${Number(addr).toString(16).toUpperCase()}`:'--'],
        ['Answered on',c.lux_bus==null?'--':(busNames[c.lux_bus]||`bus ${c.lux_bus}`)],
        ['On the bus',bus==null?'--':`${bus} device${bus===1?'':'s'}`],
        ['Reads','absolute lux — no calibration span to satisfy'],
        ['Bus levels','SDA '+(c.sda_level??'--')+' · SCL '+(c.scl_level??'--')]];
    if(c.i2c_bus0!=null||c.i2c_bus1!=null||c.i2c_bus2!=null)
      kv.splice(3,0,['Scan per bus',`Wire ${c.i2c_bus0??'--'} · Wire1 ${c.i2c_bus1??'--'} · Wire2 ${c.i2c_bus2??'--'}`]);
    return {...base,h:'good',line:`${num(lux,0)} lx`,big:`${num(lux,0)} lx`,kv:kv,
    diag:['good','Digital lux, so the divider fault class that broke the LDR twice cannot occur here. The bus row names which physical I²C controller answered — Wire is not the silkscreen pads on this variant, and that fact once cost an evening.']};
  }
  if(dead){
    // The ADC reading of the same two pins is what makes this specific. A
    // floating pin wanders, a short sits at zero, and a powered part sits at
    // the rail — but a chip whose VCC is missing gets back-powered through its
    // own ESD diodes and clamps its pins to about a diode drop. That is a
    // wire, and it names which one.
    const lvl=Math.max(c.sda_level==null?-1:c.sda_level, c.scl_level==null?-1:c.scl_level),
          backfed=lvl>60&&lvl<1200;
    return {...base,h:'bad',line:backfed?'no VCC':'no power',big:backfed?'no VCC':'no power',
      kv:[['SDA',c.sda_level==null?'LOW':`${c.sda_level} / 4095`],
          ['SCL',c.scl_level==null?'LOW':`${c.scl_level} / 4095`],
          ['On the bus',`${bus==null?'--':bus} devices`]],
      diag:['bad',backfed
        ?'Both lines sit near half a volt — not at ground, not at the rail, and not the wander of a floating pin. That is the sensor being back-powered through its own ESD diodes, which happens when SDA and SCL reach it but VCC does not. The data pair is landing; it is the power leg that is not. Check the VCC jumper at the module end and that it is in the 3.3 V pad, never 5 V.'
        :'Both lines read LOW before I2C touched them, and a powered BH1750 holds them HIGH through its own pull-ups. This is power or ground: meter VCC at the module for 3.3 V and check GND is shared.']};
  }
  return {...base,h:'bad',line:'no ACK',big:'no ACK',
    kv:[['SDA',pulledUp?'HIGH':'--'],['SCL',sclUp?'HIGH':'--'],
        ['On the bus',bus==null?'--':`${bus} devices`]],
    diag:['bad','Nothing answered at 0x23 or 0x5C on any of the three buses. The lines are pulled up, so something is powered — check SDA and SCL are on the right pads and not swapped.']};
}

// Presence is a level, so the useful number is not the level but how often it
// held while the PIR had already dropped. That difference is the entire reason
// this part is on the bench next to a PIR rather than instead of one.
function radarDesc(c,o,hist){
  const now=c.radar_presence, seen=hist.filter(r=>r.radar_presence!=null),
        on=seen.filter(r=>r.radar_presence).length,
        holds=seen.filter(r=>r.radar_presence&&!r.occupied).length;
  const base={name:'LD2410C · presence',pin:'D2 · R1',
    store:'writes tick.radar_presence'};
  if(now==null) return {...base,h:'dim',line:'not fitted',big:'not fitted',
    kv:[['Role','holds presence for someone sitting still'],
        ['Partner','PIR cross-checks it — fan blades fool radar']],
    diag:['warn','Pin 2 has not reported. Expected until the board runs firmware that reads it.']};
  if(!seen.length) return {...base,h:'good',line:now?'present':'clear',big:now?'present':'clear',
    kv:[['Role','holds presence for someone sitting still']],
    diag:['good','Reporting. No window of history to compare against the PIR yet.']};
  const duty=on/seen.length;
  // Edges and held-time come straight off the MCU and are the two figures
  // that separate a stuck pin from a busy room -- a level alone cannot.
  // Zero edges over a long hold is a fault however plausible the level looks.
  const edges=c.radar_edges, held=c.radar_held_s;
  const heldTxt=held==null?null:held>=3600?`${num(held/3600,1)} h`
    :held>=60?`${num(held/60,0)} min`:`${num(held,0)} s`;
  const deadStuck=edges===0&&held!=null&&held>600;
  const stuck=deadStuck?['bad',`OUT has not changed once since the MCU booted and has held ${heldTxt}. A working sensor changes its mind eventually; this one has not. That is a fault in the part or its wiring, not a busy room — see the README §2c.`]
    :duty===1?['warn','Presence never cleared across the whole window. A radar that never clears is usually pointed at a fan or a curtain, or its sensitivity is at maximum.']
    :on===0?['warn','Never triggered across the window. Either nobody crossed its cone, or OUT is not landing on pin 2.']
    :['good',`Held presence through ${holds} ticks where the PIR had already dropped. That gap is the still-person case this part exists for.`];
  const kv=[['Presence',`${num(duty*100,0)}% of ${seen.length} ticks`],
        ['PIR agreed',`${num(seen.filter(r=>r.occupied).length/seen.length*100,0)}%`],
        ['Radar held, PIR did not',`${holds} ticks`]];
  if(edges!=null) kv.push(['OUT transitions',`${edges} since boot`]);
  if(heldTxt) kv.push(['Level unchanged for',heldTxt]);
  return {...base,h:stuck[0]==='bad'?'bad':stuck[0]==='warn'?'warn':'good',
    line:now?'present':'clear',
    big:now?'present':'clear',kv:kv,
    diag:stuck};
}
// `series` is TRACE's {at,value} shape. The sparkline only needs the values,
// spaced evenly by index rather than by real time -- it is a shape-of-the-day
// glance, not a chart; the pop-out modal is where real time spacing matters.
function spark(series){
  if(!series||series.length<2)return '<div class="panelspark" style="display:flex;align-items:center;justify-content:center;color:var(--faint);font-size:11px">no trace yet</div>';
  const pts=series.map(p=>p.value);
  const w=310,h=44,lo=Math.min(...pts),hi=Math.max(...pts),sp=(hi-lo)||1,step=w/(pts.length-1);
  return `<svg class="panelspark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline fill="none" stroke="#5AC8FA" stroke-width="2" points="${
    pts.map((p,i)=>`${(i*step).toFixed(1)},${(h-6-((p-lo)/sp)*(h-14)).toFixed(1)}`).join(' ')}"/></svg>`}
// {at,value} pairs, not bare numbers: a real time axis in the pop-out chart
// needs the timestamp, and spark() derives its bare-number view from the
// same series rather than a second extraction that could disagree with it.
const TRACE={
  ldr:{unit:'', get:h=>h.map(r=>({at:r.at,value:r.light_raw})).filter(p=>p.value!=null)},
  dht_in:{unit:'°C', get:h=>h.map(r=>({at:r.at,value:r.indoor_c})).filter(p=>p.value!=null)},
  dht_out:{unit:'°C', get:h=>h.map(r=>({at:r.at,value:r.outdoor_c})).filter(p=>p.value!=null)},
  cam:{unit:' people', get:h=>h.map(r=>({at:r.at,value:r.people})).filter(p=>p.value!=null)},
  pir:{unit:' PMV', get:h=>h.map(r=>({at:r.at,value:r.pmv})).filter(p=>p.value!=null)},
  // Only the ticks that carried a reading: a gap plotted as 0 lux would read
  // as a dark room, and plotting absence as a value is how the LDR lied.
  bh:{unit:' lx', get:h=>h.filter(r=>r.lux!=null).map(r=>({at:r.at,value:r.lux}))},
  radar:{unit:'', get:h=>h.filter(r=>r.radar_presence!=null).map(r=>({at:r.at,value:r.radar_presence?1:0}))},
  mq:{unit:'', get:h=>h.map(r=>({at:r.at,value:r.air_raw})).filter(p=>p.value!=null)},
};

function paint(){
  if(!STATE)return;
  const D=describe(STATE);
  PARTS.forEach(p=>{const e=D[p.id];if(!e)return;
    const dot=$(`dot-${p.id}`),val=$(`val-${p.id}`),wire=$(`w-${p.id}`);
    if(dot){dot.setAttribute('fill',HEALTH[e.h]);dot.classList.toggle('pulse',e.h==='bad')}
    const box=$(`box-${p.id}`);
    if(box){if(e.h==='dim')box.setAttribute('stroke-dasharray','6 5');
            else box.removeAttribute('stroke-dasharray')}
    if(val){val.textContent=e.line;val.setAttribute('fill',e.h==='bad'?HEALTH.bad:'#9CA7B4')}
    if(wire&&!/fan|ac|lamp/.test(p.id)){
      wire.setAttribute('stroke',HEALTH[e.h]);
      if(e.h==='dim')wire.setAttribute('stroke-dasharray','6 5');
      else wire.removeAttribute('stroke-dasharray')}});
  document.querySelectorAll('.hot').forEach(g=>g.classList.toggle('sel',g.dataset.id===SEL));
  const e=D[SEL];if(!e)return;
  const tr=TRACE[SEL]?TRACE[SEL].get(STATE.history||[]):null;
  $('detail').innerHTML=`<h3>${esc(e.name)} <span class="pill dim mono">${esc(e.pin)}</span>
      <span class="${pillClass(e.h)}">${esc(e.h==='good'?'healthy':e.h==='bad'?'fault':e.h==='warn'?'attention':'not fitted')}</span></h3>
    <div class="bigval num">${esc(e.big)}</div>
    <div class="sparkrow">${spark(tr)}${tr&&tr.length>1?`<button id="expandTrace" class="expandbtn" title="Open full history, zoomable">⤢ Expand</button>`:''}</div>
    <dl class="kv num">${e.kv.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    ${e.calc||''}
    <div class="diag ${e.diag[0]}">${esc(e.diag[1])}</div>
    ${e.ctl!=null?`<div class="ctl">${e.ctl}</div>${e.blocked?`<p class="never">${esc(e.blocked)}</p>`:''}`:''}
    ${e.bench!=null?e.bench:''}
    ${e.act?`<div class="actions">${e.act.map(([t,h])=>`<a href="${h}" target="_blank" rel="noopener">${esc(t)}</a>`).join('')}</div>`:''}
    <div class="storagemini mono">${esc(e.store)}</div>`;
  // glance strip
  const c=STATE.comfort||{},o=STATE.occupancy||{},alerts=PARTS.filter(p=>D[p.id]&&D[p.id].h==='bad');
  $('glance').innerHTML=[
    ['good',`Room ${num(c.indoor_c,1)}° · ${esc(c.state||'--')}`],
    ['cool',o.count==null?'No camera count':`${o.count} person · ${num((o.confidence||0)*100,0)}%`],
    ['dim',`Tick ${age((STATE.sample||{}).age_s)}`],
    ['dim',`DB ${num(((STATE.system||{}).database||{}).size_bytes/1048576,1)} MB`],
  ].map(([k,t])=>`<span class="${pillClass(k)}">${esc(t)}</span>`).join('')
   +(alerts.length?`<span class="pill bad click pulse" id="jump">⚠ ${alerts.length} alert${alerts.length>1?'s':''} — ${
      alerts.map(a=>esc(a.name.split(' ')[0])).join(' · ')}</span>`:'');
  const j=$('jump');if(j)j.onclick=()=>{SEL=alerts[0].id;paint()};
  const ex=$('expandTrace');
  if(ex)ex.onclick=()=>openZoomChart(e.name,tr,TRACE[SEL]?TRACE[SEL].unit:'');
}

// Reasons are GROUPED AND COUNTED rather than listed. A scrolling list of
// identical lines hides its own shape; a count makes a dominant reason — the
// thing worth investigating — the first thing on the card.
function renderPlanner(d){
  const p=d.plan||{},list=p.decisions||[];
  const counts={};list.forEach(r=>{const k=r.reason||'--';counts[k]=(counts[k]||0)+1});
  const top=Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,6);
  $('planKv').innerHTML=[
    ['state',p.state||'--'],
    ['band',p.band||`PMV ±${num(p.band_pmv,1)} · PPD ≤ ${num(p.band_ppd,0)}%`],
    ['horizon',p.horizon_min?`${num(p.horizon_min,0)} min`:'--'],
    ['worst ahead',p.worst_ppd!=null?`${num(p.worst_ppd,1)}% dissatisfied`:'--'],
    ['recent decisions',`${list.length} · acted ${list.filter(r=>r.acted).length}`],
    ['coefficients',p.fit&&p.fit.provenance?p.fit.provenance:'prior'],
    ['occupancy prior',p.occupancy_prior&&p.occupancy_prior.state?p.occupancy_prior.state:'insufficient'],
  ].map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  $('planRows').innerHTML=top.length?top.map(([reason,n])=>{
    const bad=/disagree/.test(reason),flag=/no rung holds/.test(reason),
          acted=/^horizon|holds/.test(reason);
    return `<div class="decrow ${bad?'bad':flag?'flag':acted?'acted':''}">
      <time>${n}×</time><span>${esc(reason)}</span></div>`
  }).join(''):'<div class="empty">No planner decisions recorded yet</div>';
}
// The same journal the Room page shows, with nothing dropped: the actor that
// asked, the reason as typed, the outcome word and the journal's own detail.
// A refused or held-back command is the row worth finding, so the outcome
// drives the stripe rather than being buried at the end of a sentence.
function renderActions(d){
  const rows=d.actions||[];
  $('actionRows').innerHTML=rows.length?rows.map(a=>{
    const extra=[a.detail,
      a.acknowledged==null?'no acknowledgement recorded'
        :`acknowledged: ${a.acknowledged?'yes':'no'}`].filter(Boolean).join(' · ');
    return `<div class="actrow ${ACT_STRIPE[a.outcome]||''}">
      <time>${esc(actionTime(a))}</time>
      <span><b>${esc(actionName(a))} ${esc(a.what)}</b>
        <em>${esc(a.why||'reason not recorded')}${extra?` · ${esc(extra)}`:''}</em></span>
      <span class="who">${esc(a.actor||'actor not recorded')}</span>
      <span class="out"><span class="${pillClass(OUTCOME_PILL[a.outcome]||'dim')}">${esc(a.outcome)}</span>
        <em>${esc(a.outcome_detail||'')}</em></span>
    </div>`}).join(''):'<div class="empty">No commands recorded yet</div>';
}
// Bars by bytes rather than rows: what fills a board is size, and the two rank
// differently — the health table is a third of the file on a fraction of it.
function renderStore(d){
  const sys=d.system||{},rows=((sys.storage||{}).tables||[]).slice(0,7);
  const max=rows.length?Math.max(...rows.map(r=>r.bytes||0)):1;
  $('storeBars').innerHTML=rows.length?rows.map(r=>
    `<div class="bar"><span>${esc(r.table)}</span>
      <div class="track"><div class="fill" style="width:${((r.bytes||0)/max*100).toFixed(0)}%"></div></div>
      <span class="n">${num((r.bytes||0)/1048576,1)} MB</span></div>`).join('')
    :'<div class="empty">No storage breakdown</div>';
  const db=sys.database||{},b=sys.backup||{},host=sys.host||{};
  $('storeKv').innerHTML=[
    ['database',`${num(db.size_bytes/1048576,1)} MB`],
    ['ticks',db.rows||'--'],
    ['data partition',host.data_free_bytes?`${num(host.data_free_bytes/1073741824,1)} GB free`:'--'],
    ['last backup',b.at?age(Date.now()/1000-b.at):(b.state||'--')],
  ].map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
}

async function refresh(){
  try{const r=await fetch('/api/debug/dashboard',{cache:'no-store'});
    STATE=applyOptimistic(await r.json());statebar(STATE);paint();
    renderPlanner(STATE);renderStore(STATE);renderActions(STATE);
    text('foot',`Workbench · schema ${STATE.schema_version||'?'} · generated ${
      new Date((STATE.generated_at||0)*1000).toLocaleTimeString()}`)}
  catch(e){statebar({data_state:'failed',error:'Board API unavailable'})}}
async function refreshCap(){
  try{const r=await fetch('/api/manual',{cache:'no-store'});const d=await r.json();CAP=d.capability;
    const p=$('capPill');if(p){p.textContent=CAP.available?'controls ready':CAP.detail;
      p.className=pillClass(CAP.available?'good':'blocked')}
    if(STATE)paint()}
  catch(e){CAP={available:false,detail:'control check failed'}}}
bindDeviceTaps(()=>{refresh();setTimeout(refresh,2500);setTimeout(refresh,6000)});
// Refresh sooner than the device taps do: a bench pulse is over in under a
// second, so the state that matters has already changed by the first poll.
bindMotorBench(()=>{setTimeout(refresh,600);setTimeout(refresh,2500)});
drawParts();refresh();refreshCap();setInterval(refresh,10000);setInterval(refreshCap,30000);
</script>
</body></html>'''


class Handler(dashboard.Handler):
    """Same API, same commands, different HTML.

    `/workbench` is the engineering route. It is an alias rather than a rename
    so that anything already pointing at `/debug` keeps working, and so the word
    "debug" never has to appear in the Room page's own navigation — that page is
    a public surface and its vocabulary is scanned.
    """

    public_page = ROOM_PAGE
    debug_page = BENCH_PAGE
    log_page = LOG_PAGE

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/workbench":
            self.path = "/debug" + (f"?{parsed.query}" if parsed.query else "")
        super().do_GET()


def serve(host: str = os.environ.get("BREEZEIQ_HOST", "127.0.0.1"),
          port: int = DEFAULT_PORT) -> None:
    print(f"BreezeIQ console v2 -> http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
