import { useState, useEffect, useCallback, useRef } from 'react';
import axios from 'axios';
import { Upload, Loader2, Check, X, AlertTriangle, Send, RotateCcw, Ban, Sparkles, FileText } from 'lucide-react';
import { createPdfReport, reportErrorMessage } from './pdfReport';

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
const AUDIT_LABELS = { none: 'none', queued: 'queued', running: 'running', done: 'done', no_flaws: 'no flaws', text_only: 'text findings', failed: 'failed' };
const AUDIT_COLOR = { none: '#64748b', queued: '#94a3b8', running: '#38bdf8', done: '#10b981', no_flaws: '#a78bfa', text_only: '#f59e0b', failed: '#f87171' };
const clampN = (v) => {
  const n = Math.floor(Number(v));
  return Number.isFinite(n) && String(v).trim() !== '' ? Math.min(200, Math.max(1, n)) : 25;
};
const FAILED_NOTE = 'Check History before retrying: a timeout can mean the email was sent.';
// The backend audits only ready or drafted leads (audit_runner.AUDITABLE_STATUSES).
const AUDITABLE = ['ready', 'drafted'];
const RE_AUDITABLE_AUDITS = ['failed', 'no_flaws', 'text_only', 'done'];

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
  const [batchId, setBatchId] = useState(null);   // chosen once the batch list has loaded
  const [batches, setBatches] = useState(null);     // null until first load
  const [summary, setSummary] = useState(null);
  const [page, setPage] = useState({ leads: [], total: 0, facets: {} });
  const [filters, setFilters] = useState({ status: '', industry: '', seniority: '', state: '', q: '', audit_status: '' });
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(new Set());
  const [status, setStatus] = useState(null);
  const [message, setMessage] = useState('');
  const [uploading, setUploading] = useState(false);
  const [review, setReview] = useState(null);       // lead being reviewed
  const [edit, setEdit] = useState({ subject: '', body: '' });
  const [ack, setAck] = useState(false);
  const [job, setJob] = useState(null);             // { kind, done, total, failed, note, finished }
  const [drawerMessage, setDrawerMessage] = useState('');
  const [starting, setStarting] = useState(false);
  const stopRef = useRef(false);
  const runningRef = useRef(false);
  const mountedRef = useRef(true);
  const [auditN, setAuditN] = useState('25');
  const [auditProg, setAuditProg] = useState(null);  // { running, total, done, no_flaws, failed, current }
  const [auditStarting, setAuditStarting] = useState(false);
  const [reportBusy, setReportBusy] = useState(false);

  // The full design review as a PDF, for a lead who replied "yes". Takes about a minute.
  const makeReport = async (lead) => {
    setReportBusy(true);
    setDrawerMessage('');
    try {
      await createPdfReport({
        website: `https://${lead.domain}`, company: lead.organization || lead.domain, apiBase: API_BASE,
      });
    } catch (err) {
      setDrawerMessage(`PDF report failed: ${reportErrorMessage(err)}`);
    } finally {
      setReportBusy(false);
    }
  };
  const [designEdit, setDesignEdit] = useState(null); // { subject, opening, sentences: [] } for designed leads
  const [previewHtml, setPreviewHtml] = useState('');
  const [previewError, setPreviewError] = useState('');
  const previewSeq = useRef(0);
  const wasAuditing = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; stopRef.current = true; };
  }, []);
  const setJobSafe = (j) => { if (mountedRef.current) setJob(j); };

  const loadStatus = useCallback(() => {
    axios.get(`${API_BASE}/api/import/status`).then((r) => { if (mountedRef.current) setStatus(r.data); }).catch(() => {});
  }, []);

  const loadLeads = useCallback(() => {
    if (!batchId) return Promise.resolve();
    const params = { limit: PAGE, offset };
    Object.entries(filters).forEach(([k, v]) => { if (v) params[k] = v; });
    return axios.get(`${API_BASE}/api/import/batches/${batchId}/leads`, { params })
      .then((r) => {
        if (!mountedRef.current) return;
        const total = r.data.total || 0;
        if (total === 0 && offset !== 0) { setOffset(0); return; }
        if (total > 0 && offset >= total) { setOffset(Math.floor((total - 1) / PAGE) * PAGE); return; }
        setPage(r.data);
      })
      .catch((e) => { if (mountedRef.current) setMessage(errText(e)); });
  }, [batchId, filters, offset]);

  // Default pick: the stored batch if it still exists, else the newest batch with
  // work in progress (ready/drafted/approved), else the newest batch.
  const pickDefault = (list) => {
    let stored = null;
    try { stored = Number(localStorage.getItem('importBatchId')) || null; } catch { /* storage may be blocked */ }
    if (stored && list.some((b) => b.id === stored)) return stored;
    if (stored) { try { localStorage.removeItem('importBatchId'); } catch { /* ignore */ } }
    const active = list.find((b) => ['ready', 'drafted', 'approved'].some((k) => (b.status_counts?.[k] || 0) > 0));
    return (active || list[0])?.id ?? null;
  };

  // Returns the fresh list so callers can react; never throws.
  const loadBatches = useCallback(() => axios.get(`${API_BASE}/api/import/batches`)
    .then((r) => {
      const list = r.data.batches || [];
      if (mountedRef.current) setBatches(list);
      return list;
    })
    .catch((e) => { if (mountedRef.current) { setMessage(errText(e)); setBatches((b) => b || []); } return null; }), []);

  useEffect(() => {
    loadBatches().then((list) => {
      if (!list || !mountedRef.current) return;
      setBatchId((current) => (current && list.some((b) => b.id === current) ? current : pickDefault(list)));
    });
  }, [loadBatches]);

  useEffect(() => { loadStatus(); }, [loadStatus]);

  const pollAudit = useCallback(() => axios.get(`${API_BASE}/api/import/audit/progress`)
    .then((r) => { if (mountedRef.current) setAuditProg(r.data); return r.data; })
    .catch(() => null), []);
  useEffect(() => { pollAudit(); }, [pollAudit]);   // resume after a reload if an audit is still running
  const auditRunning = !!auditProg?.running;
  useEffect(() => {
    if (!auditRunning) return undefined;
    const t = setInterval(pollAudit, 3000);
    return () => clearInterval(t);
  }, [auditRunning, pollAudit]);
  useEffect(() => {
    // When an audit stops, refresh the table once so the badges settle.
    if (auditRunning) { wasAuditing.current = true; return; }
    if (wasAuditing.current) { wasAuditing.current = false; loadLeads(); loadBatches(); }
  }, [auditRunning, loadLeads, loadBatches]);

  const startAudit = async () => {
    if (!batchId || auditRunning || auditStarting) return;
    const n = clampN(auditN);
    setAuditN(String(n)); setAuditStarting(true); setMessage('');
    try {
      const r = await axios.post(`${API_BASE}/api/import/batches/${batchId}/audit`, { limit: n });
      if (mountedRef.current) {
        if (!r.data.started) setMessage('Nothing to audit: no leads are waiting, or an audit is already running.');
        await pollAudit(); loadLeads();
      }
    } catch (err) { if (mountedRef.current) setMessage(errText(err)); }
    if (mountedRef.current) setAuditStarting(false);
  };
  // Re-audit one lead (a failed, no flaws, done or out of date audit). A designed
  // email is cleared by the new result, so the lead has to be drafted again after.
  const reAudit = async (lead) => {
    if (auditRunning || auditStarting) return;
    const inDrawer = review?.id === lead.id;
    // Starting closes the drawer, and the new result clears the email anyway:
    // never throw away unsaved edits without asking. (`dirty` is read at click
    // time, from the render that drew the button.)
    if (inDrawer && dirty && !window.confirm('You have unsaved edits to this email. Re-audit anyway and discard them?')) return;
    setAuditStarting(true); setMessage(''); setDrawerMessage('');
    try {
      const r = await axios.post(`${API_BASE}/api/import/batches/${lead.batch_id || batchId}/audit`, { lead_ids: [lead.id] });
      if (mountedRef.current) {
        if (!r.data.started) {
          const note = 'Re-audit not started: another audit is running, or this lead is no longer ready or drafted.';
          if (inDrawer) setDrawerMessage(note); else setMessage(note);
        } else if (inDrawer) {
          closeReview();
        }
        await pollAudit(); loadLeads();
      }
    } catch (err) { if (mountedRef.current) { if (inDrawer) setDrawerMessage(errText(err)); else setMessage(errText(err)); } }
    if (mountedRef.current) setAuditStarting(false);
  };
  const cancelAudit = async () => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/audit/cancel`);
      if (mountedRef.current) setAuditProg(r.data);
    } catch (err) { if (mountedRef.current) setMessage(errText(err)); }
  };
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
      setFilters({ status: '', industry: '', seniority: '', state: '', q: '', audit_status: '' });
      loadStatus(); loadBatches();
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

  const chooseBatch = (id) => {
    setBatchId(id); setSummary(null); setOffset(0); setSelected(new Set()); setMessage('');
    setFilters({ status: '', industry: '', seniority: '', state: '', q: '', audit_status: '' });
    setPage({ leads: [], total: 0, facets: {} });
  };

  const toggle = (id) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const readyOnPage = page.leads.filter((l) => ['ready', 'failed'].includes(l.status));

  const draftSelected = async () => {
    if (runningRef.current) return;
    const ids = [...selected];
    if (!ids.length) return;
    runningRef.current = true;
    stopRef.current = false;
    let done = 0; let failed = 0; let note = '';
    try {
      setJobSafe({ kind: 'Drafting', done, total: ids.length, failed, note });
      for (const id of ids) {
        if (stopRef.current) { note = 'Stopped.'; break; }
        // One click: a lead that has never been audited is audited first, so the
        // draft is the designed email with its boxed screenshots, not a text one.
        const target = page.leads.find((l) => l.id === id);
        if (target && (target.audit_status || 'none') === 'none' && ['ready', 'drafted'].includes(target.status)) {
          setJobSafe({ kind: 'Drafting', done, total: ids.length, failed, note: `Auditing ${target.organization || target.domain}...` });
          try {
            // Another audit may be running; wait for it, then start this one.
            for (let i = 0; i < 100; i += 1) {
              const p = await pollAudit();
              if (!p?.running || stopRef.current) break;
              await sleep(3000);
            }
            const r = await axios.post(`${API_BASE}/api/import/batches/${target.batch_id || batchId}/audit`, { lead_ids: [id] });
            if (r.data.started) {
              await sleep(3000);
              for (let i = 0; i < 200 && !stopRef.current; i += 1) {
                const p = await pollAudit();
                if (!p?.running) break;
                await sleep(3000);
              }
            }
          } catch { /* a failed audit just means this lead is drafted as a text email */ }
          if (stopRef.current) { note = 'Stopped.'; break; }
        }
        let attempt = 0;
        let abandoned = false;
        while (attempt < 2) {
          try { await axios.post(`${API_BASE}/api/import/leads/${id}/draft`); break; } catch (err) {
            if (err.response?.status === 429 && attempt === 0) {
              // Rate limited: wait, then retry the same lead once (unless stopped meanwhile).
              attempt += 1;
              await sleep(15000);
              if (stopRef.current) { abandoned = true; break; }
              continue;
            }
            failed += 1; note = errText(err);
            if (err.response?.status === 429) { await sleep(15000); }
            break;
          }
        }
        if (abandoned) { note = 'Stopped.'; break; }
        done += 1; setJobSafe({ kind: 'Drafting', done, total: ids.length, failed, note });
      }
      setJobSafe({ kind: 'Drafting', done, total: ids.length, failed, note: note || 'Finished.', finished: true });
      if (mountedRef.current) { setSelected(new Set()); loadLeads(); loadBatches(); }
    } finally { runningRef.current = false; }
  };

  const sendApproved = async () => {
    if (runningRef.current) return;
    runningRef.current = true;
    stopRef.current = false;
    setStarting(true);
    try {
      const remaining = status?.remaining ?? 0;
      let queue = [];
      try {
        const list = await axios.get(`${API_BASE}/api/import/batches/${batchId}/leads`, { params: { status: 'approved', limit: 200 } });
        queue = list.data.leads.slice(0, remaining);
      } catch (err) { if (mountedRef.current) setMessage(errText(err)); return; }
      let done = 0; let failed = 0; let note = '';
      setJobSafe({ kind: 'Sending', done, total: queue.length, failed, note });
      if (mountedRef.current) setStarting(false);
      for (const lead of queue) {
        if (stopRef.current) { note = 'Stopped.'; break; }
        let attempt = 0;
        // The send route allows 10 per minute; the jittered pause below keeps us under it.
        while (attempt < 2) {
          if (stopRef.current) { note = note || 'Stopped.'; break; }
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
        setJobSafe({ kind: 'Sending', done, total: queue.length, failed, note });
        if (stopRef.current) break;
        await sleep(6500 + Math.random() * 2500);
      }
      setJobSafe({ kind: 'Sending', done, total: queue.length, failed, note: note || 'Finished.', finished: true });
      if (mountedRef.current) { loadStatus(); loadLeads(); loadBatches(); }
    } finally {
      runningRef.current = false;
      if (mountedRef.current) setStarting(false);
    }
  };

  const designFields = (lead) => ({
    subject: lead.subject || '',
    opening: lead.design_json?.opening || '',
    sentences: (lead.design_json?.sections || []).map((x) => x.sentence || ''),
  });
  const loadPreview = (leadId) => {
    const seq = ++previewSeq.current;
    setPreviewError('');
    return axios.get(`${API_BASE}/api/import/leads/${leadId}/preview`)
      .then((r) => { if (mountedRef.current && seq === previewSeq.current) setPreviewHtml(r.data.html || ''); })
      .catch((err) => { if (mountedRef.current && seq === previewSeq.current) { setPreviewHtml(''); setPreviewError(errText(err)); } });
  };
  const closeReview = () => { previewSeq.current += 1; setReview(null); setDrawerMessage(''); setDesignEdit(null); setPreviewHtml(''); setPreviewError(''); };
  const openReview = (lead) => {
    setDrawerMessage(''); setReview(lead); setEdit({ subject: lead.subject || '', body: lead.body || '' }); setAck(false);
    setPreviewHtml(''); setPreviewError('');
    if (lead.is_designed) { setDesignEdit(designFields(lead)); loadPreview(lead.id); } else { setDesignEdit(null); }
  };

  const saveEdit = async () => {
    try {
      const r = await axios.put(`${API_BASE}/api/import/leads/${review.id}`, edit);
      setReview(r.data.lead); replaceLead(r.data.lead); setAck(false); setDrawerMessage('');
    } catch (err) { setDrawerMessage(errText(err)); }
  };

  const saveDesign = async () => {
    try {
      const sections = designEdit.sentences.map((sentence, index) => ({ index, sentence }));
      const r = await axios.put(`${API_BASE}/api/import/leads/${review.id}/design`,
        { subject: designEdit.subject, opening: designEdit.opening, sections });
      setReview(r.data.lead); replaceLead(r.data.lead); setAck(false); setDrawerMessage('');
      setDesignEdit(designFields(r.data.lead)); loadPreview(r.data.lead.id);
    } catch (err) { setDrawerMessage(errText(err)); }
  };

  const approveReview = async () => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${review.id}/approve`, { acknowledge_warnings: ack });
      setReview(r.data.lead); replaceLead(r.data.lead); loadLeads(); setDrawerMessage('');
    } catch (err) { setDrawerMessage(errText(err)); }
  };

  const unapproveReview = async () => {
    try {
      const r = await axios.post(`${API_BASE}/api/import/leads/${review.id}/unapprove`);
      setReview(r.data.lead); replaceLead(r.data.lead); loadLeads(); setAck(false); setDrawerMessage('');
    } catch (err) { setDrawerMessage(errText(err)); }
  };

  const currentBatch = (batches || []).find((b) => b.id === batchId) || null;
  const counts = currentBatch?.status_counts || {};
  const hasWork = ['ready', 'drafted', 'approved'].some((k) => (counts[k] || 0) > 0);
  // With a fresh upload summary we know exactly how many US leads are ready. After a
  // reload there is no country data, so show the banner whenever the batch still has
  // ready, drafted or approved leads: it errs on the safe side (may show for an
  // all-India batch) but never hides a real block. /api/send enforces the real rule.
  const usBlocked = !!status && !status.mailing_address_set && (summary ? summary.us_ready > 0 : hasWork);
  const designDirty = !!review?.is_designed && !!designEdit && JSON.stringify(designEdit) !== JSON.stringify(designFields(review));
  const textDirty = !!review && !review.is_designed && (edit.subject !== (review.subject || '') || edit.body !== (review.body || ''));
  const dirty = designDirty || textDirty;
  const facets = page.facets || {};
  const busy = (!!job && !job.finished) || starting;
  const approvedCount = facets.status?.approved || 0;
  const sendDisabled = busy || !status || status.remaining <= 0 || approvedCount === 0 || usBlocked || !!status?.bounce?.paused;

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
        <label style={{ ...btn('#10b981', uploading || busy), cursor: uploading ? 'wait' : (busy ? 'not-allowed' : 'pointer') }}>
          {uploading ? <Loader2 size={14} /> : <Upload size={14} />} Choose CSV file
          <input type="file" accept=".csv,text/csv" onChange={handleFile} style={{ display: 'none' }} disabled={uploading || busy} />
        </label>
        {status && (
          <span style={{ color: '#cbd5e1', fontSize: 13 }}>
            Sent today {status.sent_today} of {status.daily_limit}, {status.remaining} left
            {status.bounce?.sent > 0 && ` · bounce rate ${status.bounce.rate_pct}% over ${status.bounce.sent} sent`}
          </span>
        )}
      </div>

      {batches && batches.length > 0 && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ color: '#94a3b8', fontSize: 13 }}>Batch</span>
          <select style={{ ...input, maxWidth: '100%' }} value={batchId || ''} disabled={busy}
            onChange={(e) => chooseBatch(Number(e.target.value))}>
            {batches.map((b) => {
              const c = b.status_counts || {};
              return (
                <option key={b.id} value={b.id}>
                  {`${b.filename} · ${String(b.created_at || '').slice(0, 10)} · ${c.ready || 0} ready, ${c.drafted || 0} drafted, ${c.approved || 0} approved, ${c.sent || 0} sent`}
                </option>
              );
            })}
          </select>
        </div>
      )}

      {(summary || currentBatch) && (
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <div style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>Total rows</div><b style={{ fontSize: 22 }}>{summary ? summary.total : currentBatch.total_rows}</b></div>
          <div style={card}><div style={{ color: '#94a3b8', fontSize: 12 }}>Ready to draft</div><b style={{ fontSize: 22, color: '#38bdf8' }}>{summary ? summary.ready : (counts.ready || 0)}</b></div>
          {Object.entries(summary ? (summary.skip_breakdown || {}) : (facets.skip_reason || {})).map(([reason, n]) => (
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
            <select style={input} value={filters.audit_status} onChange={(e) => { setFilters({ ...filters, audit_status: e.target.value }); setOffset(0); }}>
              <option value="">All audit states</option>
              {Object.entries(facets.audit_status || {}).map(([k, n]) => <option key={k} value={k}>{AUDIT_LABELS[k] || k} ({n})</option>)}
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

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <span style={{ color: '#94a3b8', fontSize: 13 }}>Audit next</span>
            <input type="number" min={1} max={200} style={{ ...input, width: 80 }} value={auditN} disabled={auditRunning || auditStarting}
              onChange={(e) => setAuditN(e.target.value)} onBlur={() => setAuditN(String(clampN(auditN)))} />
            <span style={{ color: '#94a3b8', fontSize: 13 }}>sites</span>
            <button style={btn('#a78bfa', auditRunning || auditStarting)} disabled={auditRunning || auditStarting} onClick={startAudit}>
              {auditStarting ? <Loader2 size={14} /> : <Sparkles size={14} />} Start audit
            </button>
            {auditRunning && <button style={btn('#f87171')} onClick={cancelAudit}><X size={14} /> Cancel audit</button>}
            {auditProg && (auditRunning || auditProg.total > 0) && (
              <span style={{ color: '#cbd5e1', fontSize: 13 }}>
                {auditRunning ? 'Auditing' : 'Last audit'}: {(auditProg.done || 0) + (auditProg.no_flaws || 0) + (auditProg.failed || 0)} of {auditProg.total}
                {` (${auditProg.done || 0} done, ${auditProg.no_flaws || 0} no flaws, ${auditProg.failed || 0} failed)`}
                {auditRunning && auditProg.current ? `, now ${auditProg.current}` : ''}
              </span>
            )}
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
                  <th style={{ padding: 8 }}>Status</th><th style={{ padding: 8 }}>Audit</th><th style={{ padding: 8 }} />
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
                    <td style={{ padding: 8, whiteSpace: 'nowrap', color: AUDIT_COLOR[l.audit_status || 'none'] || '#cbd5e1' }}>
                      <span title={l.audit_error || undefined}>{AUDIT_LABELS[l.audit_status || 'none'] || l.audit_status}</span>
                    </td>
                    <td style={{ padding: 8, whiteSpace: 'nowrap' }}>
                      {['drafted', 'approved', 'failed'].includes(l.status) && l.body && (
                        <button style={btn('#a78bfa')} onClick={() => openReview(l)}>Review</button>
                      )}{' '}
                      {l.status === 'ready' && RE_AUDITABLE_AUDITS.includes(l.audit_status) && (
                        <button style={btn('#a78bfa', auditRunning || auditStarting)} disabled={auditRunning || auditStarting}
                          onClick={() => reAudit(l)}><RotateCcw size={12} /> Re-audit</button>
                      )}{' '}
                      {l.status === 'skipped' && RESTORABLE.includes(l.skip_reason) && (
                        <button style={btn('#10b981', busy)} disabled={busy} onClick={() => act(l, 'restore')}><RotateCcw size={12} /> Restore</button>
                      )}
                      {['ready', 'drafted', 'approved', 'failed'].includes(l.status) && (
                        <button style={btn('#f59e0b', busy)} disabled={busy} onClick={() => act(l, 'skip')}>Skip</button>
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
          onClick={() => closeReview()}>
          <div style={{ width: 'min(560px, 96vw)', background: '#0f172a', borderLeft: '1px solid rgba(255,255,255,0.15)', padding: 24, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 12 }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <h3 style={{ margin: 0 }}>{review.person_name} · {review.organization}</h3>
              <button style={{ background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer' }} onClick={() => closeReview()}><X size={18} /></button>
            </div>
            <div style={{ color: '#94a3b8', fontSize: 12 }}>
              {review.person_title} · {review.email} · status {review.status} · audit {AUDIT_LABELS[review.audit_status || 'none'] || review.audit_status}
              {review.audited_at ? ` (${String(review.audited_at).slice(0, 10)})` : ''}
              {review.audit_error && <div style={{ color: '#fbbf24', marginTop: 2 }}>{review.audit_error}</div>}
            </div>
            {drawerMessage && <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5' }}>{drawerMessage}</div>}
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

            {review.is_designed && designEdit ? (
              <>
                {previewError && <div style={{ ...card, borderColor: '#f87171', color: '#fca5a5', fontSize: 12 }}>{previewError}</div>}
                {previewHtml && (
                  <iframe title="Email preview" sandbox="" srcDoc={previewHtml}
                    style={{ width: '100%', height: 480, border: '1px solid rgba(255,255,255,0.15)', borderRadius: 8, background: '#fff' }} />
                )}
                <label style={{ color: '#94a3b8', fontSize: 12 }}>Subject</label>
                <input style={input} value={designEdit.subject} onChange={(e) => setDesignEdit({ ...designEdit, subject: e.target.value })} />
                <label style={{ color: '#94a3b8', fontSize: 12 }}>Opening</label>
                <textarea style={{ ...input, minHeight: 80, fontFamily: 'inherit', lineHeight: 1.5 }} value={designEdit.opening}
                  onChange={(e) => setDesignEdit({ ...designEdit, opening: e.target.value })} />
                {designEdit.sentences.map((sentence, i) => (
                  <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <label style={{ color: '#94a3b8', fontSize: 12 }}>{review.design_json?.sections?.[i]?.title || `Section ${i + 1}`}</label>
                    <textarea style={{ ...input, minHeight: 60, fontFamily: 'inherit', lineHeight: 1.5 }} value={sentence}
                      onChange={(e) => setDesignEdit({ ...designEdit, sentences: designEdit.sentences.map((x, j) => (j === i ? e.target.value : x)) })} />
                  </div>
                ))}
              </>
            ) : (
              <>
                <input style={input} value={edit.subject} onChange={(e) => setEdit({ ...edit, subject: e.target.value })} />
                <textarea style={{ ...input, minHeight: 280, fontFamily: 'inherit', lineHeight: 1.5 }} value={edit.body}
                  onChange={(e) => setEdit({ ...edit, body: e.target.value })} />
              </>
            )}
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button style={btn('#38bdf8', !dirty)} disabled={!dirty} onClick={review.is_designed ? saveDesign : saveEdit}>Save edit</button>
              {review.status === 'approved' ? (
                <button style={btn('#f59e0b')} onClick={unapproveReview}>Un-approve</button>
              ) : (
                <button style={btn('#10b981', dirty || (review.review_warnings?.length > 0 && !ack))}
                  disabled={dirty || (review.review_warnings?.length > 0 && !ack)} onClick={approveReview}>
                  <Check size={14} /> Approve
                </button>
              )}
              {AUDITABLE.includes(review.status) && (
                <button style={btn('#a78bfa', auditRunning || auditStarting)} disabled={auditRunning || auditStarting}
                  title="Take new screenshots of this lead's website. A designed email is cleared and must be drafted again."
                  onClick={() => reAudit(review)}>
                  {auditStarting ? <Loader2 size={14} /> : <RotateCcw size={14} />} Re-audit
                </button>
              )}
              <button style={btn('#f97316', reportBusy)} disabled={reportBusy}
                title="Visit this lead's website and make the full design review as a PDF to send when they reply. Takes about a minute."
                onClick={() => makeReport(review)}>
                {reportBusy ? <Loader2 size={14} /> : <FileText size={14} />} {reportBusy ? 'Making PDF...' : 'PDF report'}
              </button>
              {dirty && review.status !== 'approved' && <span style={{ color: '#fbbf24', fontSize: 12, alignSelf: 'center' }}>Save your edit before approving</span>}
            </div>
            {review.status === 'approved' && review.is_designed && (
              <div style={{ color: '#94a3b8', fontSize: 12 }}>To take new screenshots, Un-approve this lead, then use Re-audit.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
