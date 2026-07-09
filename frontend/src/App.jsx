import { useState } from 'react';
import axios from 'axios';
import { Search, Zap, Send, Loader2, X, Check, Activity, BarChart, FileText } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import './App.css';

const API_BASE = ""; // Use relative paths so it works on same domain

function App() {
  const [niche, setNiche] = useState('Digital Marketing Agency');
  const [city, setCity] = useState('Mumbai');
  const [leads, setLeads] = useState([]);
  const [loadingSearch, setLoadingSearch] = useState(false);

  const handleSearch = async (e) => {
    e.preventDefault();
    setLoadingSearch(true);
    setLeads([]);
    try {
      const res = await axios.post(`${API_BASE}/api/search`, { niche, city, limit: 10 });
      setLeads(res.data.leads.map(lead => ({ ...lead, auditState: 'none' })));
    } catch (err) {
      alert("Error searching leads.");
    } finally {
      setLoadingSearch(false);
    }
  };

  const handleAudit = async (index) => {
    const lead = leads[index];
    
    // Optimistic UI update
    const newLeads = [...leads];
    newLeads[index].auditState = 'auditing';
    setLeads(newLeads);

    try {
      const res = await axios.post(`${API_BASE}/api/audit`, {
        company: lead.Company,
        website: lead.Website,
        instagram_handle: lead['Instagram Handle']
      });
      
      const updatedLeads = [...leads];
      if (res.data.error) {
        updatedLeads[index].auditState = 'failed';
      } else {
        updatedLeads[index].auditState = 'done';
        updatedLeads[index].auditData = res.data;
      }
      setLeads(updatedLeads);
    } catch (err) {
      const updatedLeads = [...leads];
      updatedLeads[index].auditState = 'failed';
      setLeads(updatedLeads);
    }
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
      finalLeads[index].auditState = 'done'; // Revert back to done so they can retry
      setLeads(finalLeads);
    }
  };

  const handleReject = (index) => {
    const newLeads = [...leads];
    newLeads[index].auditState = 'rejected';
    setLeads(newLeads);
  };

  return (
    <div className="app-container">
      <div className="bg-glow-left"></div>
      <div className="bg-glow-right"></div>
      
      <header className="header">
        <div className="logo-container">
          <Zap className="logo-icon" />
          <h1>Lead Audit AI</h1>
        </div>
        <p className="subtitle">Automated Web Scraping, AI Auditing & Outreach</p>
      </header>

      <main className="main-content">
        <form className="search-box glass" onSubmit={handleSearch}>
          <div className="input-group">
            <label>Business Niche</label>
            <input 
              type="text" 
              value={niche} 
              onChange={e => setNiche(e.target.value)} 
              placeholder="e.g. Digital Marketing Agency"
              required 
            />
          </div>
          <div className="input-group">
            <label>City</label>
            <input 
              type="text" 
              value={city} 
              onChange={e => setCity(e.target.value)} 
              placeholder="e.g. Mumbai"
              required 
            />
          </div>
          <button type="submit" className="primary-btn" disabled={loadingSearch}>
            {loadingSearch ? <Loader2 className="spin" /> : <Search />}
            {loadingSearch ? 'Scraping Google Maps...' : 'Find Leads'}
          </button>
        </form>

        <div className="leads-grid">
          <AnimatePresence>
            {leads.map((lead, i) => (
              <motion.div 
                key={i} 
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.9 }}
                transition={{ duration: 0.4, delay: i * 0.1 }}
                className={`lead-card glass ${lead.auditState === 'rejected' ? 'rejected' : ''}`}
              >
                <div className="lead-header">
                  <h3>{lead.Company}</h3>
                  {lead.auditState === 'sent' && <span className="badge success"><Check size={14}/> Sent</span>}
                  {lead.auditState === 'rejected' && <span className="badge danger"><X size={14}/> Rejected</span>}
                </div>
                
                <div className="lead-details">
                  <p><strong>URL:</strong> <a href={lead.Website} target="_blank" rel="noreferrer">{lead.Website || 'N/A'}</a></p>
                  <p><strong>Address:</strong> {lead.Address}</p>
                </div>

                {lead.auditState === 'none' && lead.Website && (
                  <button className="audit-btn" onClick={() => handleAudit(i)}>
                    <Activity size={18} /> Generate AI Audit & Email Draft
                  </button>
                )}

                {!lead.Website && (
                  <p className="error-text">Cannot audit — no website found.</p>
                )}

                {lead.auditState === 'auditing' && (
                  <div className="auditing-state">
                    <Loader2 className="spin" size={24} />
                    <p>Scraping website, running lighthouse, drafting with Claude...</p>
                  </div>
                )}

                {lead.auditState === 'failed' && (
                  <p className="error-text">Audit failed (Website unreachable or AI error).</p>
                )}

                {lead.auditState === 'done' && lead.auditData && (
                  <motion.div 
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    className="audit-results"
                  >
                    <div className="stats-row">
                      <div className="stat-box">
                        <Zap size={16} />
                        <span>Speed</span>
                        <strong>{lead.auditData.page_speed_score}/100</strong>
                      </div>
                      <div className="stat-box">
                        <BarChart size={16} />
                        <span>SEO</span>
                        <strong>{lead.auditData.seo_score}/100</strong>
                      </div>
                    </div>

                    <div className="email-draft">
                      <h4><FileText size={16} /> Drafted Email</h4>
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
                      <button className="reject-btn" onClick={() => handleReject(i)}>
                        <X size={18} /> Reject
                      </button>
                      <button className="send-btn" onClick={() => handleSend(i)}>
                        <Send size={18} /> Approve & Send
                      </button>
                    </div>
                  </motion.div>
                )}
                
                {lead.auditState === 'sending' && (
                  <div className="auditing-state">
                    <Loader2 className="spin" size={24} />
                    <p>Sending email via AWS SES...</p>
                  </div>
                )}
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      </main>
    </div>
  );
}

export default App;
