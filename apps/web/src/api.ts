export type Spec = any
export interface Template { id: string; name: string; notes: string; underlying: string; kind: string; spec: Spec }
export interface Validation { ok: boolean; errors: { field: string; message: string }[]; risk?: any }
export interface Job { id: string; status: 'queued' | 'running' | 'done' | 'error'; progress: number; at?: string; error?: string; run_id?: string; result?: RunResult; seconds?: number }
export interface RunResult { strategy: Spec; start: string; end: string; metrics: any; daily: [string, number][]; diagnostics: any; trades: any[]; trade_count: number }
export interface RunRow { run_id: string; name: string; start: string; end: string; net_pnl: number; trades: number; win_rate_pct: number; max_drawdown: number; worst_day: number }

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
  return r.json()
}
export const api = {
  status: () => fetch('/api/status').then(j<any>),
  templates: () => fetch('/api/templates').then(j<Template[]>),
  validate: (spec: Spec) => fetch('/api/strategies/validate', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(spec) }).then(j<Validation>),
  strategies: () => fetch('/api/strategies').then(j<Spec[]>),
  save: (spec: Spec) => fetch('/api/strategies', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(spec) }).then(j<any>),
  backtest: (spec: Spec, start: string, end: string, where = 'server') => fetch('/api/backtests', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ spec, start, end, where }) }).then(j<{ id: string }>),
  job: (id: string) => fetch(`/api/backtests/${id}`).then(j<Job>),
  runs: () => fetch('/api/runs').then(j<RunRow[]>),
  run: (id: string) => fetch(`/api/runs/${id}`).then(j<RunResult>),
  broker: () => fetch('/api/broker').then(j<any>),
  market: () => fetch('/api/market').then(j<any>),
  paper: () => fetch('/api/paper').then(j<any>),
  paperGet: (id: string) => fetch(`/api/paper/${encodeURIComponent(id)}`).then(j<any>),
  paperStart: (strategy_id: string, mode = 'paper') => fetch('/api/paper', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ strategy_id, mode }) }).then(j<{ id: string }>),
  paperCalendar: (mode = 'paper') => fetch(`/api/paper-calendar?mode=${mode}`).then(j<Record<string, { net: number; cycles: number }>>),
  live: () => fetch('/api/live').then(j<any>),
  arm: (id: string, confirm: string, override_gate: boolean, override_reason = '') => fetch(`/api/live/${encodeURIComponent(id)}/arm`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ confirm, override_gate, override_reason }) }).then(j<any>),
  disarm: (id: string) => fetch(`/api/live/${encodeURIComponent(id)}/disarm`, { method: 'POST' }).then(j<any>),
  kill: () => fetch('/api/live/kill', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ command_id: 'kill' + Date.now() }) }).then(j<any>),
  exitLeg: (legId: number) => fetch(`/api/live/exit/${legId}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ command_id: `exit${legId}-${Date.now()}` }) }).then(j<any>),
  paperStop: (id: string) => fetch(`/api/paper/${encodeURIComponent(id)}/stop`, { method: 'POST' }).then(j<any>),
  data: () => fetch('/api/data').then(j<any>),
}
