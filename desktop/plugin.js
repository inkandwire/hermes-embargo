// Embargo Desktop half: renders `::embargo{...}` (appended by the Python hook) as a teletype stamp with a live countdown.
// Attributes are untrusted model output: every field is validated, bad input renders nothing. Python decides what is held; this only shows it.
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const STATES = {
  held: { stamp: 'HELD FOR RELEASE', tone: 'red' },
  detected: { stamp: 'EMBARGO DETECTED', tone: 'amber' },
  lifted: { stamp: 'EMBARGO LIFTED', tone: 'green' },
  unset: { stamp: 'NO RELEASE TIME', tone: 'grey' }
}
const num = (v, max = 12) => (new RegExp(`^\\d{1,${max}}$`).test(v ?? '') ? Number(v) : null)
const clean = s => (s ?? '').replace(/[^\x20-\x7E\u00A0-\uFFFF]/g, '').slice(0, 80)
const pad = n => String(n).padStart(2, '0')

function clock(sec) {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60)
  return `${d ? `${d}d ` : ''}${pad(h)}:${pad(m)}:${pad(sec % 60)}`
}

const CSS = `
.emb{--ink:#1b1a17;--paper:#f3efe3;--red:#b3261e;--amber:#8a5300;--green:#1b6b3a;--grey:#5b5a55;max-width:30rem;margin:.5rem 0;padding:.875rem 1rem;display:grid;gap:.5rem;
  font:600 .8125rem/1.35 ui-monospace,'SF Mono','Cascadia Mono',Menlo,Consolas,monospace;color:var(--ink);background:var(--paper);border-radius:.25rem;
  background-image:repeating-linear-gradient(0deg,transparent 0 1.35rem,rgb(0 0 0/.045) 1.35rem calc(1.35rem + 1px));box-shadow:0 0 0 1px rgb(0 0 0/.25)}
.emb[data-tone=red]{--tone:var(--red)}.emb[data-tone=amber]{--tone:var(--amber)}.emb[data-tone=green]{--tone:var(--green)}.emb[data-tone=grey]{--tone:var(--grey)}
.emb-top{display:flex;justify-content:space-between;align-items:flex-start;gap:.75rem}
.emb-stamp{color:var(--tone);border:.375rem double var(--tone);padding:.25rem .625rem;font-size:1rem;font-weight:800;letter-spacing:.08em;transform:rotate(-2.5deg);background:rgb(255 255 255/.35)}
@media (prefers-reduced-motion:no-preference){.emb-stamp{animation:emb-slam .28s cubic-bezier(.2,.9,.3,1.2) both}@keyframes emb-slam{from{transform:rotate(-2.5deg) scale(1.9);opacity:0}to{transform:rotate(-2.5deg) scale(1);opacity:1}}}
.emb-id{color:var(--paper);background:var(--ink);font-size:.8125rem;font-weight:700;padding:.125rem .5rem;border-radius:.25rem}
.emb-label{font-weight:700;font-size:.875rem;overflow-wrap:anywhere}
.emb-count{font-size:1.75rem;line-height:1.1;letter-spacing:.04em;font-variant-numeric:tabular-nums;color:var(--tone)}
.emb-dim{color:#33322e;font-weight:600;font-size:1rem}
.emb-dim code{font:inherit;font-weight:700;background:rgb(0 0 0/.09);padding:.0625rem .3rem;border-radius:.1875rem}`

function Card({ attrs }) {
  const a = attrs || {}
  const spec = STATES[a.state]
  const id = /^E\d{1,4}$/.test(a.id ?? '') ? a.id : null
  const until = num(a.until)
  const n = num(a.n, 4)
  const tool = /^[\w.-]{1,40}$/.test(a.tool ?? '') ? a.tool : null
  const tz = /^[A-Za-z0-9+:-]{1,12}$/.test(a.tz ?? '') ? a.tz : null
  const live = !!until && (a.state === 'held' || a.state === 'detected')
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000))
  useEffect(() => {
    if (!live) return undefined
    const t = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000)
    return () => clearInterval(t)
  }, [live])
  if (!spec || !id) return null // bad input renders nothing
  const left = live ? Math.max(0, until - now) : null
  const cleared = live && left === 0
  const tone = cleared ? 'green' : spec.tone
  const stamp = cleared ? 'CLEARED' : spec.stamp
  const when = until ? new Date(until * 1000).toLocaleString([], { weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }) : null
  const label = clean(a.label)
  return jsxs('section', { className: 'emb', 'data-tone': tone, 'aria-label': `Embargo ${id}: ${stamp}`, children: [
    jsx('style', { children: CSS }),
    jsxs('div', { className: 'emb-top', children: [jsx('span', { className: 'emb-stamp', children: stamp }), jsx('span', { className: 'emb-id', children: id })] }),
    label && jsx('div', { className: 'emb-label', children: label }),
    left !== null && jsx('div', { className: 'emb-count', role: 'timer', children: clock(left) }),
    cleared && jsx('div', { className: 'emb-dim', children: 'Ask again to send.' }),
    a.state === 'unset' && jsxs('div', { className: 'emb-dim', children: ['Set a release time: ', jsx('code', { children: `/embargo set ${id} 09:00` })] }),
    a.state === 'lifted' && jsx('div', { className: 'emb-dim', children: 'The hold is over. Ask the agent to send it again.' }),
    when && a.state !== 'lifted' && jsx('div', { className: 'emb-dim', children: `Lifts ${when}` }),
    when && a.state !== 'lifted' && tz && jsx('div', { className: 'emb-dim', children: `Release said ${tz}` }),
    when && a.state === 'lifted' && jsx('div', { className: 'emb-dim', children: `Lifted ${when}${n ? ` · ${n} blocked` : ''}` }),
    n && a.state !== 'lifted' && jsx('div', { className: 'emb-dim', children: `${n} blocked${tool ? `, last: ${tool}` : ''}` })
  ] })
}

export default {
  id: 'embargo', name: 'Embargo',
  register(ctx) {
    ctx.register({ id: 'card', area: 'transcript.directives', data: { name: 'embargo', render: ({ attrs }) => jsx(Card, { attrs }) } })
  }
}
