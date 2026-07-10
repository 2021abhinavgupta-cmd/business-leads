import { useState, useRef, useEffect } from 'react';
import axios from 'axios';
import { Search, Zap, Send, Loader2, X, Check, Activity, BarChart, FileText, Home, Clock, DollarSign, LayoutDashboard, Calendar, FileEdit } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import './App.css';

const API_BASE = ""; // Use relative paths so it works on same domain

function App() {
  const [currentView, setCurrentView] = useState('home');
  const [niche, setNiche] = useState('');
  const [city, setCity] = useState('');
  const [limit, setLimit] = useState(10);
  const [leads, setLeads] = useState(() => {
    const saved = localStorage.getItem('leadAuditLeads');
    return saved ? JSON.parse(saved) : [];
  });
  const [loadingSearch, setLoadingSearch] = useState(false);
  const [manualCompany, setManualCompany] = useState('');
  const [manualWebsite, setManualWebsite] = useState('');
  const [showManualEntry, setShowManualEntry] = useState(false);
  const [isAutopilot, setIsAutopilot] = useState(false);
  
  const [historyLogs, setHistoryLogs] = useState([]);
  const [costLogs, setCostLogs] = useState([]);
  const [drafts, setDrafts] = useState([]);
  const [expandedEmail, setExpandedEmail] = useState(null);

  const isAutopilotRef = useRef(false);
  const leadsRef = useRef([]);

  // Calculate dynamic exact cost based on backend tracking for local session
  const sessionTotalCost = leads.reduce((acc, lead) => {
    let cost = 0;
    if (lead.search_cost) cost += parseFloat(lead.search_cost);
    if (lead.auditState === 'done' || lead.auditState === 'sent' || lead.auditState === 'sending') {
      if (lead.auditData && lead.auditData.ai_cost) cost += parseFloat(lead.auditData.ai_cost);
      else cost += 0.0001; 
    }
    if (lead.auditState === 'sent') cost += 0.0001;
    return acc + cost;
  }, 0);

  useEffect(() => {
    isAutopilotRef.current = isAutopilot;
  }, [isAutopilot]);

  useEffect(() => {
    leadsRef.current = leads;
    localStorage.setItem('leadAuditLeads', JSON.stringify(leads));
  }, [leads]);

  // Fetch DB data when view changes
  useEffect(() => {
    const t = Date.now();
    if (currentView === 'history') {
      axios.get(`${API_BASE}/api/history?t=${t}`).then(res => setHistoryLogs(res.data.history)).catch(console.error);
    }
    if (currentView === 'cost') {
      axios.get(`${API_BASE}/api/costs?t=${t}`).then(res => setCostLogs(res.data.costs)).catch(console.error);
    }
    if (currentView === 'drafts') {
      axios.get(`${API_BASE}/api/drafts?t=${t}`).then(res => setDrafts(res.data.drafts)).catch(console.error);
    }
  }, [currentView]);

  // Globally fetch costs on mount and periodically so the total cost pill is always accurate
  useEffect(() => {
    const fetchCosts = () => {
      axios.get(`${API_BASE}/api/costs?t=${Date.now()}`).then(res => setCostLogs(res.data.costs)).catch(console.error);
    };
    fetchCosts();
    const interval = setInterval(fetchCosts, 5000);
    return () => clearInterval(interval);
  }, []);

  const totalAllTime = costLogs.reduce((acc, log) => acc + log.cost, 0);

  const handleSearch = async (e) => {
    e.preventDefault();
    setLoadingSearch(true);
    setLeads([]);
    try {
      const res = await axios.post(`${API_BASE}/api/search`, { niche, city, limit: parseInt(limit) || 10 });
      setLeads(res.data.leads.map(lead => ({ ...lead, auditState: 'none' })));
    } catch (err) {
      alert("Error searching leads.");
    } finally {
      setLoadingSearch(false);
    }
  };

  const handleAddManualLead = (e) => {
    e.preventDefault();
    if (!manualCompany || !manualWebsite) return;
    const newLead = {
      Company: manualCompany,
      Website: manualWebsite.startsWith('http') ? manualWebsite : `https://${manualWebsite}`,
      Address: 'Added Manually',
      auditState: 'none'
    };
    setLeads([newLead, ...leads]);
    setManualCompany('');
    setManualWebsite('');
  };

  const handleAudit = async (index) => {
    const lead = leadsRef.current[index];
    setLeads(prev => {
      const newLeads = [...prev];
      newLeads[index].auditState = 'auditing';
      return newLeads;
    });

    try {
      const res = await axios.post(`${API_BASE}/api/audit`, {
        company: lead.Company,
        website: lead.Website,
        instagram_handle: lead['Instagram Handle']
      });
      
      setLeads(prev => {
        const updatedLeads = [...prev];
        if (res.data.error) {
          updatedLeads[index].auditState = 'failed';
        } else {
          updatedLeads[index].auditState = 'done';
          updatedLeads[index].auditData = res.data;
        }
        return updatedLeads;
      });
    } catch (err) {
      setLeads(prev => {
        const updatedLeads = [...prev];
        updatedLeads[index].auditState = 'failed';
        return updatedLeads;
      });
    }
  };

  const startAutopilot = async () => {
    setIsAutopilot(true);
    for (let i = 0; i < leadsRef.current.length; i++) {
      if (!isAutopilotRef.current) break; 
      const lead = leadsRef.current[i];
      if (lead.auditState === 'none' && lead.Website) {
        await handleAudit(i);
      }
    }
    setIsAutopilot(false);
  };

  const handleSend = async (index) => {
    const lead = leads[index];
    if (!lead.auditData?.email) {
      alert("No email address found to send to!");
      return;
    }
    
    const updatedLeads = [...leads];
    updatedLeads[index].auditState = 'sending';
    setLeads(updatedLeads);
    
    try {
      await axios.post(`${API_BASE}/api/send`, {
        email: lead.auditData.email,
        subject: lead.auditData.subject,
        body: lead.auditData.body,
        company: lead.Company,
        website: lead.Website
      });
      
      const finalLeads = [...leads];
      finalLeads[index].auditState = 'sent';
      setLeads(finalLeads);
    } catch (err) {
      alert("Failed to send email.");
      const finalLeads = [...leads];
      finalLeads[index].auditState = 'done'; 
      setLeads(finalLeads);
    }
  };

  const handleDraftSend = async (draft, draftIndex) => {
    if (!draft.target_email) {
      alert("No email address found to send to!");
      return;
    }
    
    const originalDrafts = [...drafts];
    const newDrafts = [...drafts];
    newDrafts[draftIndex] = { ...draft, sending: true };
    setDrafts(newDrafts);

    try {
      await axios.post(`${API_BASE}/api/send`, {
        email: draft.target_email,
        subject: draft.subject,
        body: draft.body,
        company: draft.company,
        website: draft.website
      });
      
      // Remove from drafts list since it was sent
      setDrafts(drafts.filter(d => d.id !== draft.id));
    } catch (err) {
      alert("Failed to send draft email.");
      setDrafts(originalDrafts);
    }
  };

  const handleDraftDelete = async (draftId) => {
    try {
      await axios.delete(`${API_BASE}/api/drafts/${draftId}`);
      setDrafts(drafts.filter(d => d.id !== draftId));
    } catch (err) {
      alert("Failed to delete draft.");
    }
  };

  const renderHome = () => (
    <>
      <header className="header">
        <p className="subtitle">Automated Web Scraping, AI Auditing & Outreach</p>
      </header>

      <form className="search-box glass" onSubmit={handleSearch}>
        <div className="input-group">
          <label>Business Niche</label>
          <input type="text" list="niche-options" value={niche} onChange={e => setNiche(e.target.value)} placeholder="e.g. Digital Marketing Agency" required />
          <datalist id="niche-options">
            <option value="Digital Marketing Agency" />
            <option value="Software Development" />
            <option value="Dental Clinic" />
            <option value="Real Estate Agency" />
            <option value="Law Firm" />
            <option value="Accounting Firm" />
            <option value="Plumbing Services" />
          </datalist>
        </div>
        <div className="input-group">
          <label>City</label>
          <input type="text" list="city-options" value={city} onChange={e => setCity(e.target.value)} placeholder="e.g. Mumbai" required />
          <datalist id="city-options">
            <option value="Mumbai" />
            <option value="Pune" />
            <option value="Nagpur" />
            <option value="Nashik" />
            <option value="Thane" />
            <option value="Navi Mumbai" />
            <option value="Chhatrapati Sambhajinagar" />
            <option value="Kolhapur" />
            <option value="Solapur" />
          </datalist>
        </div>
        <div className="input-group" style={{maxWidth: '100px'}}>
          <label>Leads</label>
          <input type="number" value={limit} onChange={e => setLimit(e.target.value)} min="1" max="100" required />
        </div>
        <div style={{ display: 'flex', gap: '12px', alignItems: 'flex-end' }}>
          <button type="submit" className="primary-btn" disabled={loadingSearch}>
            {loadingSearch ? <Loader2 className="spin" /> : <Search />}
            {loadingSearch ? 'Scraping...' : 'Find Leads'}
          </button>
          <button type="button" onClick={() => setShowManualEntry(!showManualEntry)} style={{ background: showManualEntry ? 'rgba(239, 68, 68, 0.2)' : 'rgba(255,255,255,0.1)', border: showManualEntry ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid rgba(255,255,255,0.2)', color: showManualEntry ? '#f87171' : '#e2e8f0', padding: '0 20px', borderRadius: '12px', cursor: 'pointer', height: '48px', fontSize: '15px', fontWeight: 'bold', transition: 'all 0.2s', whiteSpace: 'nowrap' }}>
            {showManualEntry ? 'Cancel' : '+ Specific Lead'}
          </button>
        </div>
      </form>

      <AnimatePresence>
        {showManualEntry && (
          <motion.form initial={{ opacity: 0, height: 0, marginTop: 0 }} animate={{ opacity: 1, height: 'auto', marginTop: 16 }} exit={{ opacity: 0, height: 0, marginTop: 0 }} className="search-box glass" style={{ overflow: 'hidden' }} onSubmit={handleAddManualLead}>
            <div className="input-group">
              <label>Specific Company Name</label>
              <input type="text" value={manualCompany} onChange={e => setManualCompany(e.target.value)} placeholder="e.g. Acme Corp" />
            </div>
            <div className="input-group">
              <label>Website URL</label>
              <input type="text" value={manualWebsite} onChange={e => setManualWebsite(e.target.value)} placeholder="e.g. acme.com" />
            </div>
            <button type="submit" className="primary-btn" style={{ background: '#10b981' }}>+ Add Lead</button>
          </motion.form>
        )}
      </AnimatePresence>

      {leads.length > 0 && (
        <div className="actions-bar" style={{ display: 'flex', justifyContent: 'center', marginBottom: '20px' }}>
          <button 
            className={`primary-btn ${isAutopilot ? 'danger' : ''}`} 
            onClick={() => isAutopilot ? setIsAutopilot(false) : startAutopilot()}
            style={{ background: isAutopilot ? '#ef4444' : 'var(--primary-color)' }}
          >
            <Activity className={isAutopilot ? 'spin' : ''} />
            {isAutopilot ? 'Stop Autopilot' : 'Start Autopilot (Audit All)'}
          </button>
        </div>
      )}

      <div className="leads-grid">
        <AnimatePresence>
          {leads.map((lead, i) => (
            <motion.div key={i} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: 0.9 }} className={`lead-card glass ${lead.auditState === 'rejected' ? 'rejected' : ''}`}>
              <div className="lead-header">
                <h3>{lead.Company}</h3>
                {lead.auditState === 'sent' && <span className="badge success"><Check size={14}/> Sent</span>}
                {lead.auditState === 'rejected' && <span className="badge danger"><X size={14}/> Rejected</span>}
              </div>
              
              <div className="lead-details">
                <p><strong>URL:</strong> <a href={lead.Website} target="_blank" rel="noreferrer">{lead.Website || 'N/A'}</a></p>
                <p><strong>Address:</strong> {lead.Address}</p>
                {lead.auditState === 'sent' && lead.auditData && (
                  <div style={{ marginTop: '12px', padding: '8px', background: 'rgba(16, 185, 129, 0.1)', borderRadius: '6px', border: '1px solid rgba(16, 185, 129, 0.2)' }}>
                    <p style={{ color: '#059669', marginBottom: '4px' }}><strong>To:</strong> {lead.auditData.email}</p>
                    <p style={{ color: '#475569', fontSize: '0.9em' }}><strong>From:</strong> {lead.auditData.sender_email || 'System'}</p>
                  </div>
                )}
              </div>

              {lead.auditState === 'none' && lead.Website && (
                <button className="audit-btn" onClick={() => handleAudit(i)}><Activity size={18} /> Generate AI Audit & Draft</button>
              )}

              {!lead.Website && <p className="error-text">Cannot audit — no website found.</p>}
              {lead.auditState === 'auditing' && <div className="auditing-state"><Loader2 className="spin" size={24} /><p>Running analysis...</p></div>}
              {lead.auditState === 'failed' && <p className="error-text">Audit failed.</p>}
              {lead.auditState === 'sending' && <div className="auditing-state"><Loader2 className="spin" size={24} /><p>Sending via SES...</p></div>}

              {lead.auditState === 'done' && lead.auditData && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} className="audit-results">
                  <div className="stats-row">
                    <div className="stat-box"><Zap size={16} /><span>Speed</span><strong>{lead.auditData.page_speed_score}/100</strong></div>
                    <div className="stat-box"><BarChart size={16} /><span>SEO</span><strong>{lead.auditData.seo_score}/100</strong></div>
                  </div>
                  <div className="email-draft">
                    <h4><FileText size={16} /> Drafted Email</h4>
                    {lead.auditData.image_url && (
                      <div style={{ marginBottom: '16px', textAlign: 'center' }}>
                        <img src={lead.auditData.image_url} alt="Website Screenshot" style={{ maxWidth: '100%', maxHeight: '400px', borderRadius: '8px', border: '2px solid #ef4444' }} />
                      </div>
                    )}
                    <p className="target-email"><strong>To:</strong> {lead.auditData.email || 'Email not found (will fail)'}</p>
                    <p className="subject"><strong>Subject:</strong> {lead.auditData.subject}</p>
                    <textarea 
                      className="email-body-editor" 
                      value={lead.auditData.body} 
                      onChange={(e) => {
                        const newLeads = [...leads];
                        newLeads[i].auditData.body = e.target.value;
                        setLeads(newLeads);
                      }} 
                    />
                  </div>
                  <div className="action-buttons">
                    <button className="reject-btn" onClick={() => {
                      const newLeads = [...leads];
                      newLeads[i].auditState = 'rejected';
                      setLeads(newLeads);
                    }}><X size={18} /> Reject</button>
                    <button className="send-btn" onClick={() => handleSend(i)}><Send size={18} /> Approve & Send</button>
                  </div>
                </motion.div>
              )}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </>
  );

  const renderDrafts = () => (
    <div className="glass" style={{ padding: '24px' }}>
      <h2><FileEdit style={{display:'inline', marginRight: '8px', verticalAlign: 'middle'}}/> Saved Drafts</h2>
      <p style={{color: '#94a3b8', marginBottom: '24px'}}>AI-generated audits ready for your review and approval.</p>
      
      <div className="leads-grid" style={{ gridTemplateColumns: '1fr' }}>
        <AnimatePresence>
          {drafts.map((draft, i) => (
            <motion.div key={draft.id} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: 0.9 }} className="lead-card glass">
              <div className="lead-header">
                <h3>{draft.company}</h3>
                <span className="badge" style={{background: 'rgba(245, 158, 11, 0.2)', color: '#f59e0b', border: '1px solid rgba(245, 158, 11, 0.4)'}}>Draft</span>
              </div>
              <div className="lead-details">
                <p><strong>URL:</strong> <a href={draft.website} target="_blank" rel="noreferrer">{draft.website}</a></p>
                <p><strong>To:</strong> {draft.target_email || 'Missing email'}</p>
              </div>

              {draft.sending ? (
                <div className="auditing-state"><Loader2 className="spin" size={24} /><p>Sending via SES...</p></div>
              ) : (
                <div className="email-draft" style={{ marginTop: '16px' }}>
                  {draft.image_url && (
                    <div style={{ marginBottom: '16px', textAlign: 'center' }}>
                      <img src={draft.image_url} alt="Website Screenshot" style={{ maxWidth: '100%', maxHeight: '400px', borderRadius: '8px', border: '2px solid #ef4444' }} />
                    </div>
                  )}
                  <p className="subject" style={{marginBottom: '8px'}}><strong>Subject:</strong> {draft.subject}</p>
                  <textarea 
                    className="email-body-editor" 
                    value={draft.body} 
                    onChange={(e) => {
                      const newDrafts = [...drafts];
                      newDrafts[i].body = e.target.value;
                      setDrafts(newDrafts);
                    }} 
                  />
                  <div className="action-buttons" style={{marginTop: '16px'}}>
                    <button className="reject-btn" onClick={() => handleDraftDelete(draft.id)}><X size={18} /> Discard</button>
                    <button className="send-btn" onClick={() => handleDraftSend(draft, i)}><Send size={18} /> Approve & Send</button>
                  </div>
                </div>
              )}
            </motion.div>
          ))}
        </AnimatePresence>
        {drafts.length === 0 && <p style={{textAlign: 'center', color: '#64748b', padding: '40px 0'}}>No saved drafts.</p>}
      </div>
    </div>
  );

  const renderHistory = () => (
    <div className="glass" style={{ padding: '24px' }}>
      <h2><Clock style={{display:'inline', marginRight: '8px', verticalAlign: 'middle'}}/> Email Sent History</h2>
      <p style={{color: '#94a3b8', marginBottom: '24px'}}>Persistent log of all outbound emails dispatched.</p>
      
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {historyLogs.map(log => (
          <div key={log.id} style={{ background: 'rgba(255,255,255,0.02)', padding: '16px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <h4 style={{ margin: '0 0 4px 0' }}>{log.company}</h4>
                <p style={{ margin: 0, fontSize: '13px', color: '#64748b' }}>To: {log.target_email} • From: {log.sender_email}</p>
              </div>
              <div style={{ textAlign: 'right' }}>
                <span style={{ fontSize: '12px', color: '#10b981', display: 'block' }}>{log.timestamp}</span>
                <button 
                  style={{ background: 'none', border: 'none', color: '#ef4444', cursor: 'pointer', fontSize: '13px', padding: '4px 0' }}
                  onClick={() => setExpandedEmail(expandedEmail === log.id ? null : log.id)}
                >
                  {expandedEmail === log.id ? 'Hide Content' : 'View Content'}
                </button>
              </div>
            </div>
            
            {expandedEmail === log.id && (
              <div style={{ marginTop: '16px', padding: '16px', background: 'rgba(0,0,0,0.2)', borderRadius: '6px' }}>
                <p style={{ margin: '0 0 12px 0', fontSize: '14px', fontWeight: 'bold' }}>Subject: {log.subject}</p>
                <p style={{ margin: 0, fontSize: '13px', whiteSpace: 'pre-wrap', lineHeight: '1.6' }}>{log.body}</p>
              </div>
            )}
          </div>
        ))}
        {historyLogs.length === 0 && <p style={{textAlign: 'center', color: '#64748b', padding: '40px 0'}}>No emails sent yet.</p>}
      </div>
    </div>
  );

  const renderCost = () => {
    return (
      <div className="glass" style={{ padding: '24px' }}>
        <h2><DollarSign style={{display:'inline', marginRight: '8px', verticalAlign: 'middle'}}/> Lifetime Cost Dashboard</h2>
        <p style={{color: '#94a3b8', marginBottom: '24px'}}>Exact fractional penny tracking pulled from AI provider headers & API metadata.</p>
        
        <div style={{ display: 'flex', gap: '24px', marginBottom: '32px' }}>
          <div style={{ flex: 1, padding: '24px', background: 'rgba(16, 185, 129, 0.1)', border: '1px solid rgba(16, 185, 129, 0.3)', borderRadius: '12px', textAlign: 'center' }}>
            <span style={{ fontSize: '14px', color: '#059669', textTransform: 'uppercase', letterSpacing: '1px' }}>Total Pipeline Cost</span>
            <div style={{ fontSize: '48px', fontWeight: 'bold', color: '#10b981', margin: '12px 0' }}>${totalAllTime.toFixed(5)}</div>
          </div>
        </div>

        <h3><Calendar style={{display:'inline', marginRight: '8px', verticalAlign: 'middle'}} size={18}/> Audit Trail</h3>
        <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: '16px', fontSize: '14px' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)', color: '#94a3b8' }}>
              <th style={{ textAlign: 'left', padding: '12px 8px' }}>Timestamp</th>
              <th style={{ textAlign: 'left', padding: '12px 8px' }}>Category</th>
              <th style={{ textAlign: 'left', padding: '12px 8px' }}>Description</th>
              <th style={{ textAlign: 'right', padding: '12px 8px' }}>Cost ($)</th>
            </tr>
          </thead>
          <tbody>
            {costLogs.map(log => (
              <tr key={log.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                <td style={{ padding: '12px 8px', color: '#cbd5e1' }}>{log.timestamp}</td>
                <td style={{ padding: '12px 8px', color: '#38bdf8' }}>{log.category}</td>
                <td style={{ padding: '12px 8px', color: '#94a3b8' }}>{log.description}</td>
                <td style={{ padding: '12px 8px', textAlign: 'right', color: '#10b981', fontFamily: 'monospace' }}>{log.cost.toFixed(5)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {costLogs.length === 0 && <p style={{textAlign: 'center', color: '#64748b', padding: '40px 0'}}>No costs accrued yet.</p>}
      </div>
    );
  };

  return (
    <div className="app-container" style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh', padding: 0 }}>
      <div className="bg-glow-left"></div>
      <div className="bg-glow-right"></div>
      
      {/* Top Navigation */}
      <nav style={{ background: 'rgba(15, 23, 42, 0.7)', backdropFilter: 'blur(20px)', border: '1px solid rgba(255,255,255,0.1)', padding: '12px 32px', display: 'flex', flexDirection: 'row', alignItems: 'center', gap: '32px', zIndex: 10, borderRadius: '50px', margin: '24px auto 0 auto', width: '92%', maxWidth: '1200px', boxShadow: '0 8px 32px rgba(0,0,0,0.2)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', color: '#fff' }}>
          <Zap className="logo-icon" size={28} />
          <h1 style={{ margin: 0, fontSize: '20px', letterSpacing: '-0.5px' }}>Lead Audit AI</h1>
        </div>
        
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginLeft: 'auto' }}>
          <button 
            onClick={() => setCurrentView('home')}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: currentView === 'home' ? 'rgba(255,255,255,0.15)' : 'transparent', border: 'none', borderRadius: '8px', color: currentView === 'home' ? '#fff' : '#94a3b8', cursor: 'pointer', fontSize: '15px', fontWeight: currentView === 'home' ? 'bold' : 'normal', transition: 'all 0.2s' }}
          >
            <Home size={18} /> Dashboard
          </button>
          <button 
            onClick={() => setCurrentView('drafts')}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: currentView === 'drafts' ? 'rgba(255,255,255,0.15)' : 'transparent', border: 'none', borderRadius: '8px', color: currentView === 'drafts' ? '#fff' : '#94a3b8', cursor: 'pointer', fontSize: '15px', fontWeight: currentView === 'drafts' ? 'bold' : 'normal', transition: 'all 0.2s' }}
          >
            <FileEdit size={18} /> Drafts
          </button>
          <button 
            onClick={() => setCurrentView('cost')}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: currentView === 'cost' ? 'rgba(255,255,255,0.15)' : 'transparent', border: 'none', borderRadius: '8px', color: currentView === 'cost' ? '#fff' : '#94a3b8', cursor: 'pointer', fontSize: '15px', fontWeight: currentView === 'cost' ? 'bold' : 'normal', transition: 'all 0.2s' }}
          >
            <LayoutDashboard size={18} /> Costs
          </button>
          <button 
            onClick={() => setCurrentView('history')}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: currentView === 'history' ? 'rgba(255,255,255,0.15)' : 'transparent', border: 'none', borderRadius: '8px', color: currentView === 'history' ? '#fff' : '#94a3b8', cursor: 'pointer', fontSize: '15px', fontWeight: currentView === 'history' ? 'bold' : 'normal', transition: 'all 0.2s' }}
          >
            <Clock size={18} /> History
          </button>
          
          {/* Global Total Cost Pill in Navbar */}
          <div 
            style={{ marginLeft: '16px', background: 'rgba(16, 185, 129, 0.1)', padding: '6px 16px', borderRadius: '50px', border: '1px solid rgba(16, 185, 129, 0.3)', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}
            onClick={() => setCurrentView('cost')}
          >
            <span style={{ fontSize: '11px', color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '1px' }}>Cost</span>
            <span style={{ fontSize: '16px', fontWeight: 'bold', color: '#10b981' }}>${totalAllTime.toFixed(5)}</span>
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <main className="main-content" style={{ flex: 1, padding: '40px', overflowY: 'auto' }}>
        {currentView === 'home' && renderHome()}
        {currentView === 'drafts' && renderDrafts()}
        {currentView === 'history' && renderHistory()}
        {currentView === 'cost' && renderCost()}
      </main>
    </div>
  );
}

export default App;
