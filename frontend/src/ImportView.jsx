import { useState, useEffect, useCallback, useRef } from 'react';
import axios from 'axios';
import { Upload, Loader2, Check, X, AlertTriangle, Send, RotateCcw, Ban, Sparkles } from 'lucide-react';

const API_BASE = '';
const PAGE = 50;
const SKIP_LABELS = {
  invalid_email: 'Invalid email', unsupported_country: 'Unsupported country',
  duplicate_in_file: 'Duplicate in file', already_imported: 'Already imported',
  suppressed: 'Unsubscribed', already_emailed: 'Already emailed',
  company_already_emailed: 'Company already emailed', gov_domain: 'Government',
  second_contact: 'Second contact', manual: 'Skipped by you',
};
const RESTORABLE = ['gov_domain', 'second_contact', 'manual'];
const STATUS_COLOR = {
  ready: '#38bdf8', drafted: '#a78bfa', approved: '#10b981', sent: '#64748b',
  failed: '#f87171', skipped: '#f59e0b',
};
const FAILED_NOTE = 'Check History before retrying: a timeout can mean the email was sent.';

const errText = (err) => {
  const d = err?.response?.data?.detail;
  if (!d) return err?.message || 'Request failed';
  if (typeof d === 'string') return d;
  return d.message || JSON.stringify(d);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const card = { padding: 16, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 12 };
const btn = (color = '#38bdf8', disabled = false) => ({
  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8,
  border: `1px solid ${color}`, background: 'transparent', color, cursor: disabled ? 'not-allowed' : 'pointer',
  opacity: disabled ? 0.45 : 1, fontSize: 13,
});
const input = { background: 'rgba(15,23,42,0.6)', border: '1px solid rgba(255,255,255,0.15)', borderRadius: 8, color: '#e2e8f0', padding: '8px 10px', fontSize: 13 };

export default function ImportView() {
  const [batchId, setBatchId] = useState(() => {
    try { return Number(localStorage.getItem('importBatchId')) || null; } catch { return null; }
  });
  const [summary, setSummary] = useState(null);
  const [page, setPage] = useState({ leads: [], total: 0, facets: {} });
  const [filters, setFilters] = useState({ status: '', industry: '', seniority: '', state: '', q: '' });
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(new Set());
  const [status, setStatus] = useState(null);
  const [message, setMessage] = useState('');
  const [uploading, setUploading] = useState(false);
  const [review, setReview] = useState(null);       // lead being reviewed
  const [edit, setEdit] = useState({ subject: '', body: '' });
  const [ack, setAck] = useState(false);
  const [job, setJob] = useState(null);             // { kind, done, total, failed, note, finished }
  const stopRef = useRef(false);

  const loadStatus = useCallback(() => {
    axios.get(`${API_BASE}/api/import/status`).then((r) => setStatus(r.data)).catch(() => {});
  }, []);

  const loadLeads = useCallback(() => {
    if (!batchId) return Promise.resolve();
    const params = { limit: PAGE, offset };
    Object.entries(filters).forEach(([k, v]) => { if (v) params[k] = v; });
    return axios.get(`${API_BASE}/api/import/batches/${batchId}/leads`, { params })
      .then((r) => setPage(r.data))
      .catch((e) => setMessage(errText(e)));
  }, [batchId, filters, offset]);

  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => { loadLeads(); }, [loadLeads]);
  useEffect(() => {
    if (!batchId) return;
    try { localStorage.setItem('importBatchId', String(batchId)); } catch { /* storage may be blocked */ }
  }, [batchId]);

  const handleFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setUploading(true); setMessage('');
    try {
      const csv_text = await file.text();
      const r = await axios.post(`${API_BASE}/api/import/upload`, { filename: file.name, csv_text });
      setSummary(r.data); setBatchId(r.data.batch_id); setOffset(0); setSelected(new Set());
      setFilters({ status: '', industry: '', seniority: '', state: '', q: '' });
      loadStatus();
    } catch (err) { setMessage(errText(err)); }
    setUploading(false);
  };

  const replaceLead = (lead) => setPage((p) => ({ ...p, leads: p.leads.map((l) => (l.id === lead.id ? lead : l)) }));

  const act = async (lead, action) => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${lead.id}/${action}`);
      replaceLead(r.data.lead); loadLeads();
    } catch (err) { setMessage(errText(err)); loadLeads(); }
  };

  const toggle = (id) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const readyOnPage = page.leads.filter((l) => ['ready', 'failed'].includes(l.status));

  const draftSelected = async () => {
    const ids = [...selected];
    if (!ids.length) return;
    stopRef.current = false;
    let done = 0; let failed = 0; let note = '';
    setJob({ kind: 'Drafting', done, total: ids.length, failed, note });
    for (const id of ids) {
      if (stopRef.current) { note = 'Stopped.'; break; }
      try { await axios.post(`${API_BASE}/api/import/leads/${id}/draft`); } catch (err) {
        failed += 1; note = errText(err);
        if (err.response?.status === 429) { await sleep(15000); }
      }
      done += 1; setJob({ kind: 'Drafting', done, total: ids.length, failed, note });
    }
    setJob({ kind: 'Drafting', done, total: ids.length, failed, note: note || 'Finished.', finished: true });
    setSelected(new Set()); loadLeads();
  };

  const sendApproved = async () => {
    stopRef.current = false;
    const remaining = status?.remaining ?? 0;
    let queue = [];
    try {
      const list = await axios.get(`${API_BASE}/api/import/batches/${batchId}/leads`, { params: { status: 'approved', limit: 200 } });
      queue = list.data.leads.slice(0, remaining);
    } catch (err) { setMessage(errText(err)); return; }
    let done = 0; let failed = 0; let note = '';
    setJob({ kind: 'Sending', done, total: queue.length, failed, note });
    for (const lead of queue) {
      if (stopRef.current) { note = 'Stopped.'; break; }
      let attempt = 0;
      // The send route allows 10 per minute; the jittered pause below keeps us under it.
      while (attempt < 2) {
        try {
          await axios.post(`${API_BASE}/api/send`, {
            email: lead.email, subject: lead.subject, body: lead.body,
            company: lead.organization || lead.domain, website: lead.domain,
            import_lead_id: lead.id, acknowledge_warnings: true,
          });
          done += 1; break;
        } catch (err) {
          const detail = errText(err);
          if (err.response?.status === 429 && /Daily sending limit/i.test(detail)) { note = detail; stopRef.current = true; break; }
          if (err.response?.status === 429 && attempt === 0) { attempt += 1; await sleep(20000); continue; }
          if (err.response?.data?.detail?.code === 'import_paused') { note = detail; stopRef.current = true; break; }
          failed += 1; note = detail; break;
        }
      }
      setJob({ kind: 'Sending', done, total: queue.length, failed, note });
      if (stopRef.current) break;
      await sleep(6500 + Math.random() * 2500);
    }
    setJob({ kind: 'Sending', done, total: queue.length, failed, note: note || 'Finished.', finished: true });
    loadStatus(); loadLeads();
  };

  const openReview = (lead) => { setReview(lead); setEdit({ subject: lead.subject || '', body: lead.body || '' }); setAck(false); };

  const saveEdit = async () => {
    try {
      const r = await axios.put(`${API_BASE}/api/import/leads/${review.id}`, edit);
      setReview(r.data.lead); replaceLead(r.data.lead); setAck(false); setMessage('');
    } catch (err) { setMessage(errText(err)); }
  };

  const approveReview = async () => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${review.id}/approve`, { acknowledge_warnings: ack });
      setReview(r.data.lead); replaceLead(r.data.lead); loadLeads(); setMessage('');
    } catch (err) { setMessage(errText(err)); }
  };

  const unapproveReview = async () => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${review.id}/unapprove`);
      setReview(r.data.lead); replaceLead(r.data.lead); loadLeads();
    } catch (err) { setMessage(errText(err)); }
  };

  const usBlocked = !!status && !status.mailing_address_set && (summary ? summary.us_ready > 0 : true);
  const facets = page.facets || {};
  const busy = !!job && !job.finished;
  const approvedCount = facets.status?.approved || 0;
  const sendDisabled = busy || approvedCount === 0 || usBlocked || !!status?.bounce?.paused;

  return (
    <div className="glass" style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div>
        <h2 style={{ margin: 0 }}><Upload size={22} style={{ verticalAlign: 'middle', marginRight: 8 }} />Import leads from CSV</h2>
        <p style={{ color: '#94a3b8', margin: '6px 0 0' }}>
          Upload a lead list, let the AI draft one email per lead, read and approve each draft, then send a day's worth at a time.
        </p>
      </div>

      {usBlocked && (
        <div style={{ ...card, borderColor: '#f59e0b', color: '#fbbf24' }}>
          <AlertTriangle size={16} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          US leads cannot be sent yet: set the <b>MAILING_ADDRESS</b> environment variable (a postal address is required in the footer under CAN-SPAM).
        </div>
      )}
      {status?.bounce?.paused && (
        <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5' }}>
          <Ban size={16} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          Import sending is paused: {status.bounce.bounced} of {status.bounce.sent} bounced ({status.bounce.rate_pct}%, limit {status.bounce.pause_pct}%).
        </div>
      )}
      {message && <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5' }}>{message}</div>}

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <label style={{ ...btn('#10b981', uploading), cursor: uploading ? 'wait' : 'pointer' }}>
          {uploading ? <Loader2 size={14} /> : <Upload size={14} />} Choose CSV file
          <input type="file" accept=".csv,text/csv" onChange={handleFile} style={{ display: 'none' }} disabled={uploading} />
        </label>
        {status && (
          <span style={{ color: '#cbd5e1', fontSize: 13 }}>
            Sent today {status.sent_today} of {status.daily_limit}, {status.remaining} left
            {status.bounce?.sent > 0 && ` · bounce rate ${status.bounce.rate_pct}% over ${status.bounce.sent} sent`}
          </span>
        )}
      </div>

      {summary && (
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <div style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>Total rows</div><b style={{ fontSize: 22 }}>{summary.total}</b></div>
          <div style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>Ready to draft</div><b style={{ fontSize: 22, color: '#38bdf8' }}>{summary.ready}</b></div>
          {Object.entries(summary.skip_breakdown || {}).map(([reason, n]) => (
            <div key={reason} style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>{SKIP_LABELS[reason] || reason}</div><b style={{ fontSize: 22, color: '#f59e0b' }}>{n}</b></div>
          ))}
        </div>
      )}

      {batchId && (
        <>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <select style={input} value={filters.status} onChange={(e) => { setFilters({ ...filters, status: e.target.value }); setOffset(0); }}>
              <option value="">All statuses</option>
              {Object.entries(facets.status || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <select style={input} value={filters.industry} onChange={(e) => { setFilters({ ...filters, industry: e.target.value }); setOffset(0); }}>
              <option value="">All industries</option>
              {Object.entries(facets.industry || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <select style={input} value={filters.seniority} onChange={(e) => { setFilters({ ...filters, seniority: e.target.value }); setOffset(0); }}>
              <option value="">All seniority</option>
              {Object.entries(facets.seniority || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <select style={input} value={filters.state} onChange={(e) => { setFilters({ ...filters, state: e.target.value }); setOffset(0); }}>
              <option value="">All states</option>
              {Object.entries(facets.state || {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
            <input style={{ ...input, minWidth: 180 }} placeholder="Search name, company, email" value={filters.q}
              onChange={(e) => { setFilters({ ...filters, q: e.target.value }); setOffset(0); }} />
          </div>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <button style={btn('#a78bfa', busy || selected.size === 0)} disabled={busy || selected.size === 0} onClick={draftSelected}>
              <Sparkles size={14} /> Draft {selected.size} selected
            </button>
            <button style={btn('#38bdf8', busy)} disabled={busy} onClick={() => setSelected(new Set(readyOnPage.map((l) => l.id)))}>
              Select all ready on this page
            </button>
            <button style={btn('#10b981', sendDisabled)} disabled={sendDisabled} onClick={sendApproved}>
              <Send size={14} /> Send up to {Math.min(approvedCount, status?.remaining ?? 0)} approved
            </button>
            {busy && <button style={btn('#f87171')} onClick={() => { stopRef.current = true; }}><X size={14} /> Stop</button>}
          </div>

          {job && (
            <div style={card}>
              <b>{job.kind}</b> {job.done} of {job.total}{job.failed > 0 && `, ${job.failed} failed`}
              <div style={{ height: 6, background: 'rgba(255,255,255,0.1)', borderRadius: 3, marginTop: 8 }}>
                <div style={{ height: 6, width: `${job.total ? (job.done / job.total) * 100 : 0}%`, background: '#10b981', borderRadius: 3 }} />
              </div>
              {job.note && <div style={{ color: '#94a3b8', fontSize: 12, marginTop: 6 }}>{job.note}</div>}
              {job.kind === 'Sending' && job.failed > 0 && (
                <div style={{ color: '#fbbf24', fontSize: 12, marginTop: 6 }}>{FAILED_NOTE}</div>
              )}
            </div>
          )}

          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ color: '#94a3b8', textAlign: 'left', borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
                  <th style={{ padding: 8 }} />
                  <th style={{ padding: 8 }}>Name</th><th style={{ padding: 8 }}>Title</th>
                  <th style={{ padding: 8 }}>Company</th><th style={{ padding: 8 }}>Email</th>
                  <th style={{ padding: 8 }}>Status</th><th style={{ padding: 8 }} />
                </tr>
              </thead>
              <tbody>
                {page.leads.map((l) => (
                  <tr key={l.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                    <td style={{ padding: 8 }}>
                      {['ready', 'failed'].includes(l.status) && (
                        <input type="checkbox" checked={selected.has(l.id)} onChange={() => toggle(l.id)} disabled={busy} />
                      )}
                    </td>
                    <td style={{ padding: 8 }}>{l.person_name}</td>
                    <td style={{ padding: 8, color: '#94a3b8' }}>{l.person_title}</td>
                    <td style={{ padding: 8 }}>{l.organization}</td>
                    <td style={{ padding: 8, color: '#94a3b8' }}>{l.email}</td>
                    <td style={{ padding: 8, color: STATUS_COLOR[l.status] || '#cbd5e1' }}>
                      {l.status}{l.skip_reason ? `: ${SKIP_LABELS[l.skip_reason] || l.skip_reason}` : ''}
                      {l.error && <div style={{ color: '#f87171', fontSize: 11 }}>{l.error}</div>}
                      {l.status === 'failed' && <div style={{ color: '#fbbf24', fontSize: 11 }}>{FAILED_NOTE}</div>}
                    </td>
                    <td style={{ padding: 8, whiteSpace: 'nowrap' }}>
                      {['drafted', 'approved', 'failed'].includes(l.status) && l.body && (
                        <button style={btn('#a78bfa')} onClick={() => openReview(l)}>Review</button>
                      )}{' '}
                      {l.status === 'skipped' && RESTORABLE.includes(l.skip_reason) && (
                        <button style={btn('#10b981')} onClick={() => act(l, 'restore')}><RotateCcw size={12} /> Restore</button>
                      )}
                      {['ready', 'drafted', 'approved', 'failed'].includes(l.status) && (
                        <button style={btn('#f59e0b')} onClick={() => act(l, 'skip')}>Skip</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {page.leads.length === 0 && <p style={{ textAlign: 'center', color: '#64748b', padding: 24 }}>No leads match.</p>}
          </div>

          <div style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#94a3b8', fontSize: 13 }}>
            <button style={btn('#38bdf8', offset === 0)} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
            <span>{page.total === 0 ? 0 : offset + 1} to {Math.min(offset + PAGE, page.total)} of {page.total}</span>
            <button style={btn('#38bdf8', offset + PAGE >= page.total)} disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </div>
        </>
      )}

      {review && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(2,6,23,0.7)', zIndex: 30, display: 'flex', justifyContent: 'flex-end' }}
          onClick={() => setReview(null)}>
          <div style={{ width: 'min(560px, 96vw)', background: '#0f172a', borderLeft: '1px solid rgba(255,255,255,0.15)', padding: 24, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 12 }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <h3 style={{ margin: 0 }}>{review.person_name} · {review.organization}</h3>
              <button style={{ background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer' }} onClick={() => setReview(null)}><X size={18} /></button>
            </div>
            <div style={{ color: '#94a3b8', fontSize: 12 }}>{review.person_title} · {review.email} · status {review.status}</div>
            {review.status === 'failed' && (
              <div style={{ ...card, borderColor: '#f59e0b', color: '#fbbf24', fontSize: 12 }}>
                {review.error && <div style={{ marginBottom: 4 }}>{review.error}</div>}
                {FAILED_NOTE}
              </div>
            )}

            {review.review_warnings?.length > 0 && (
              <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5' }}>
                <b><AlertTriangle size={14} style={{ verticalAlign: 'middle' }} /> Review before sending</b>
                <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>{review.review_warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
                {review.status !== 'approved' && (
                  <label style={{ display: 'flex', gap: 8, marginTop: 10, color: '#e2e8f0' }}>
                    <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I have read these warnings
                  </label>
                )}
              </div>
            )}
            {review.source_quote && (
              <div style={{ ...card, color: '#94a3b8', fontSize: 12 }}>Evidence from their homepage: “{review.source_quote}”</div>
            )}

            <input style={input} value={edit.subject} onChange={(e) => setEdit({ ...edit, subject: e.target.value })} />
            <textarea style={{ ...input, minHeight: 280, fontFamily: 'inherit', lineHeight: 1.5 }} value={edit.body}
              onChange={(e) => setEdit({ ...edit, body: e.target.value })} />
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button style={btn('#38bdf8', edit.subject === review.subject && edit.body === review.body)}
                disabled={edit.subject === review.subject && edit.body === review.body} onClick={saveEdit}>Save edit</button>
              {review.status === 'approved' ? (
                <button style={btn('#f59e0b')} onClick={unapproveReview}>Un-approve</button>
              ) : (
                <button style={btn('#10b981', review.review_warnings?.length > 0 && !ack)}
                  disabled={review.review_warnings?.length > 0 && !ack} onClick={approveReview}>
                  <Check size={14} /> Approve
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
