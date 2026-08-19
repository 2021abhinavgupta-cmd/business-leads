import { useState, useRef, useEffect } from 'react';
import axios from 'axios';
import { Search, Zap, Send, Loader2, X, Check, Activity, BarChart, FileText, Home, Clock, DollarSign, LayoutDashboard, Calendar, FileEdit, MapPin, Eye, EyeOff, MessageSquare, MessageCircle, AlertTriangle, RefreshCw, Sprout, HelpCircle } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { NICHES, CITIES } from './searchOptions';
import { AGRI_NICHES } from './agriNiches';
import './App.css';

// Strips everything but digits, since wa.me only accepts a bare
// countrycode+number — the "+91 98765 43210" GBP format doesn't work as-is.
function phoneToWhatsAppDigits(phone) {
  return (phone || '').replace(/\D/g, '');
}

// Per-tab help content for the "?" button in the nav. Keyed by currentView,
// so the button always explains the page you're actually looking at rather
// than one generic help screen nobody reads past the first line.
const HELP_CONTENT = {
  home: {
    title: 'Dashboard — finding & auditing leads',
    tips: [
      'Type a niche (e.g. "Dental Clinic") and a city, then Find Leads — this hits Google Maps and only returns businesses that have a website, since that\'s what gets audited.',
      'No niche in mind? Use "Find Leads Near Me" — it searches every business type around your current location, no typing needed.',
      'Click "Generate AI Audit & Draft" on a lead to run the full analysis and produce a ready-to-edit email. This takes 1-2 minutes per lead — the progress bar shows which stage it\'s on.',
      'Always read the subject and body before sending — edit either one inline. A red "Review before sending" banner means an accuracy check flagged something specific; read it, don\'t just dismiss it.',
      'Leads with no website show a WhatsApp button instead, since there\'s nothing to audit.',
      '"Start Autopilot" runs the audit step on every un-audited lead in the list automatically, one at a time.',
    ],
  },
  agriculture: {
    title: 'Agriculture — targeting the agriculture sector specifically',
    tips: [
      'Set a city first — Maps and directory searches both need one (the government dealer list is the one exception; leave city blank there to pull from all of Maharashtra).',
      'Click a niche card\'s "Maps" button for Google-Maps-sourced leads (needs the business to have a website to be audit-able).',
      'The colored "Directory" button searches whichever B2B directory is selected in the dropdown above (IndiaMART / TradeIndia / ExportersIndia, or "All Directories" to search all three) — good for suppliers with no website of their own.',
      '"Search licensed dealers" pulls from Maharashtra\'s own government-published dealer list — real phone/email already filled in, no guessing, but no website either, so use the WhatsApp button on those leads rather than the audit flow.',
      '"Search All Niches" runs Maps + your selected directory across every preset niche automatically, one request at a time — takes several minutes on purpose (keeps it under the API\'s rate limits), leads land in your list as it goes, and you can Stop it early any time.',
      'You can click several niches/sources in a row — results just add to your lead list, they don\'t replace it. Switch to Dashboard yourself when ready to review.',
      'Emails to irrigation/tractor/farm-equipment leads automatically mention the relevant real government subsidy scheme (PM-KUSUM / SMAM) instead of generic copy.',
    ],
  },
  drafts: {
    title: 'Drafts — reviewing saved audits before sending',
    tips: [
      'This is everything that\'s been audited but not yet sent or rejected — safe to leave leads here while you decide.',
      'A draft can go stale: if too many days pass since the audit ran, sending it will ask you to confirm first, since the site may have changed since the findings were written.',
      'You can remove the attached screenshot from a draft without discarding the whole email, if the capture looks bad (mid-animation, wrong crop, etc).',
    ],
  },
  cost: {
    title: 'Costs — what this tool has actually spent',
    tips: [
      'Every row here is a real logged cost — Google Maps API calls, AI audit calls, AWS SES sends — not an estimate.',
      'The running total in the top-right pill updates every few seconds and reflects the same numbers as this page.',
      'If a number here looks off, check for a lead that got audited more than once (a "Retry Audit" click after a failure does cost again).',
    ],
  },
  history: {
    title: 'History — sent emails and reply performance',
    tips: [
      'Every email actually sent (not just drafted) shows up here, with open/reply status if tracking is enabled for this deployment.',
      '"Check for replies" scans the connected inbox for genuine replies — auto-replies and bounces are detected and excluded, not counted as engagement.',
      'The variant performance table compares reply rates between different email copy styles — treat any row under ~20 sends as too little data to draw a conclusion from yet.',
    ],
  },
};

const API_BASE = ""; // Use relative paths so it works on same domain

// Sent as X-API-Key on every request; backend only enforces it if API_KEY is
// set server-side (see config.py / .env.example). Set VITE_API_KEY in
// frontend/.env to match the backend's API_KEY before building for production.
if (import.meta.env.VITE_API_KEY) {
  axios.defaults.headers.common['X-API-Key'] = import.meta.env.VITE_API_KEY;
}

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
  const [loadingNearby, setLoadingNearby] = useState(false);
  const [nearbyRadiusKm, setNearbyRadiusKm] = useState(5);
  const [manualCompany, setManualCompany] = useState('');
  const [manualWebsite, setManualWebsite] = useState('');
  const [showManualEntry, setShowManualEntry] = useState(false);
  const [isAutopilot, setIsAutopilot] = useState(false);
  const [agriCity, setAgriCity] = useState('');
  const [agriSearchKey, setAgriSearchKey] = useState(''); // "<niche>|<source>" currently loading, disables just that button
  const [b2bDirectory, setB2bDirectory] = useState('indiamart'); // which directory the "Directory" button dorks
  const [agriLastResult, setAgriLastResult] = useState(''); // feedback after a search, without leaving the tab
  const [agriBulkRunning, setAgriBulkRunning] = useState(false);
  const [agriBulkProgress, setAgriBulkProgress] = useState(null); // { current, total, label }
  const [showHelp, setShowHelp] = useState(false);
  
  const [leadsPage, setLeadsPage] = useState(1);
  const LEADS_PAGE_SIZE = 12;

  const [historyLogs, setHistoryLogs] = useState([]);
  const [trackingEnabled, setTrackingEnabled] = useState(false);
  const [replyCheckEnabled, setReplyCheckEnabled] = useState(false);
  const [checkingReplies, setCheckingReplies] = useState(false);
  const [variantPerf, setVariantPerf] = useState([]);
  const [currentVariant, setCurrentVariant] = useState('');
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
    setShowHelp(false);
    const t = Date.now();
    if (currentView === 'history') {
      axios.get(`${API_BASE}/api/history?t=${t}`).then(res => {
        setHistoryLogs(res.data.history);
        setTrackingEnabled(!!res.data.tracking_enabled);
        setReplyCheckEnabled(!!res.data.reply_checking_enabled);
        setVariantPerf(res.data.variant_performance || []);
        setCurrentVariant(res.data.current_variant || '');
      }).catch(console.error);
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
    setLeadsPage(1);
    try {
      const res = await axios.post(`${API_BASE}/api/search`, { niche, city, limit: parseInt(limit) || 10 });
      setLeads(res.data.leads.map(lead => ({ ...lead, auditState: 'none' })));
    } catch (err) {
      console.error('Search failed:', err);
      alert(`Error searching leads: ${err.response?.data?.detail || err.message}`);
    } finally {
      setLoadingSearch(false);
    }
  };

  const B2B_DIRECTORIES = ['indiamart', 'tradeindia', 'exportersindia'];

  // One real request against one source. Returns how many leads it added.
  // Pulled out of handleAgriSearch so both the per-card button and the bulk
  // "Search All Niches" runner share the exact same fetch/tag/merge logic.
  const runOneAgriSearch = async (nicheChoice, source, directoryOverride) => {
    const res = source === 'directory'
      ? await axios.post(`${API_BASE}/api/search-b2b-directory`, { niche: nicheChoice, city: agriCity, limit: parseInt(limit) || 10, directory: directoryOverride })
      : await axios.post(`${API_BASE}/api/search`, { niche: nicheChoice, city: agriCity, limit: parseInt(limit) || 10 });
    const tagged = res.data.leads.map(lead => ({ ...lead, auditState: 'none', sector: 'agriculture', sectorDetail: nicheChoice }));
    setLeads(prev => [...tagged, ...prev]);
    return tagged.length;
  };

  // Shared by every Agriculture-tab niche button. Tags results with
  // sector: 'agriculture' (client-side only) so handleAudit forwards it to
  // /api/audit, which flows into BaseSender.generate_email's sector line.
  // Deliberately stays on this tab rather than jumping to Dashboard — the
  // earlier version did that after every click, which meant clicking a
  // second niche meant navigating back to Agriculture again first. Leads
  // still land in the same shared `leads` array; switch to Dashboard
  // yourself when you're done queuing up searches.
  const handleAgriSearch = async (nicheChoice, source) => {
    if (!agriCity.trim()) {
      alert('Enter a city first.');
      return;
    }
    const key = `${nicheChoice}|${source}`;
    setAgriSearchKey(key);
    try {
      let added = 0;
      let label;
      if (source === 'directory' && b2bDirectory === 'all') {
        // "All" isn't a real backend value (B2BDirectorySearchRequest.directory
        // is a Literal of the three actual sites) — fire one request per site
        // instead. Sequential, not Promise.all: keeps requests spaced out
        // rather than bursting 3 at once against the same rate-limit bucket.
        for (const dir of B2B_DIRECTORIES) {
          added += await runOneAgriSearch(nicheChoice, 'directory', dir);
        }
        label = 'all directories';
      } else {
        added = await runOneAgriSearch(nicheChoice, source, b2bDirectory);
        label = source === 'directory' ? b2bDirectory : 'Maps';
      }
      setLeadsPage(1);
      setAgriLastResult(`Added ${added} lead${added === 1 ? '' : 's'} for "${nicheChoice}" (${label}). Switch to Dashboard to review.`);
    } catch (err) {
      console.error('Agriculture search failed:', err);
      alert(`Error searching leads: ${err.response?.data?.detail || err.message}`);
    } finally {
      setAgriSearchKey('');
    }
  };

  // Runs Maps + the selected directory/directories across every preset niche,
  // one request at a time. Paced ~13s apart regardless of which endpoint —
  // simplest safe margin under /api/search's and /api/search-b2b-directory's
  // separate 5-requests-per-60s limits (app.py's rate_limit), since a real
  // batch like this is exactly what would otherwise trip them. Stoppable
  // mid-run via agriBulkStopRef, same ref-based pattern as Autopilot uses to
  // avoid a stale closure reading an old "still running" flag.
  const agriBulkStopRef = useRef(false);
  const handleSearchAllNiches = async () => {
    if (!agriCity.trim()) {
      alert('Enter a city first.');
      return;
    }
    agriBulkStopRef.current = false;
    setAgriBulkRunning(true);

    const directories = b2bDirectory === 'all' ? B2B_DIRECTORIES : [b2bDirectory];
    const jobs = [];
    for (const niche of AGRI_NICHES) {
      jobs.push({ niche, source: 'maps' });
      for (const dir of directories) jobs.push({ niche, source: 'directory', dir });
    }

    let totalAdded = 0;
    try {
      for (let i = 0; i < jobs.length; i++) {
        if (agriBulkStopRef.current) break;
        const { niche, source, dir } = jobs[i];
        setAgriBulkProgress({ current: i + 1, total: jobs.length, label: `${niche} (${source === 'directory' ? dir : 'Maps'})` });
        try {
          totalAdded += await runOneAgriSearch(niche, source, dir);
        } catch (err) {
          console.error(`Bulk search failed for ${niche} (${source}):`, err);
          // One failed niche/source shouldn't stop the whole run — errors are
          // rare (a transient DDG rate limit, one bad Maps query) and losing
          // 71 other results to one of them would be worse than skipping it.
        }
        setLeadsPage(1);
        if (i < jobs.length - 1 && !agriBulkStopRef.current) {
          await new Promise(r => setTimeout(r, 13000));
        }
      }
    } finally {
      setAgriBulkRunning(false);
      setAgriBulkProgress(null);
      setAgriLastResult(
        agriBulkStopRef.current
          ? `Stopped early — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'} before stopping. Switch to Dashboard to review.`
          : `Searched all ${AGRI_NICHES.length} niches — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'} total. Switch to Dashboard to review.`
      );
    }
  };

  // Separate from handleAgriSearch: this source takes no niche (one fixed
  // government dataset — licensed seed dealers) and city is optional
  // (narrows by district/taluka rather than being required).
  const handleKrishiMaharashtraSearch = async () => {
    setAgriSearchKey('krishi-maharashtra');
    try {
      const res = await axios.post(`${API_BASE}/api/search-krishi-maharashtra`, { city: agriCity, limit: parseInt(limit) || 50 });
      const tagged = res.data.leads.map(lead => ({ ...lead, auditState: 'none', sector: 'agriculture', sectorDetail: lead.Category || '' }));
      setLeads(prev => [...tagged, ...prev]);
      setLeadsPage(1);
      setAgriLastResult(`Added ${tagged.length} lead${tagged.length === 1 ? '' : 's'} from the licensed dealer list. Switch to Dashboard to review.`);
    } catch (err) {
      console.error('Krishi Maharashtra search failed:', err);
      alert(`Error searching leads: ${err.response?.data?.detail || err.message}`);
    } finally {
      setAgriSearchKey('');
    }
  };

  const handleSearchNearby = () => {
    if (!navigator.geolocation) {
      alert('Your browser does not support location access, so nearby search is unavailable. Use the niche and city search instead.');
      return;
    }

    setLoadingNearby(true);
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        try {
          const res = await axios.post(`${API_BASE}/api/search-nearby`, {
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            radius_m: Math.round(nearbyRadiusKm * 1000),
            limit: parseInt(limit) || 10,
          });
          setLeads(res.data.leads.map(lead => ({ ...lead, auditState: 'none' })));
          setLeadsPage(1);
          if (res.data.leads.length === 0) {
            alert('No businesses with a website were found nearby. Try a larger radius.');
          }
        } catch (err) {
          console.error('Nearby search failed:', err);
          alert(`Error searching nearby: ${err.response?.data?.detail || err.message}`);
        } finally {
          setLoadingNearby(false);
        }
      },
      (error) => {
        setLoadingNearby(false);
        // Distinguish the causes — "denied" needs a browser-settings fix,
        // the others are usually transient or environmental.
        const reason = {
          1: 'Location permission was denied. Allow location access for this site in your browser settings, then try again.',
          2: 'Your location could not be determined. Check that location services are enabled on your device.',
          3: 'Getting your location timed out. Try again.',
        }[error.code] || 'Could not get your location.';
        alert(reason);
      },
      { enableHighAccuracy: false, timeout: 15000, maximumAge: 300000 }
    );
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
    setLeadsPage(1);
    setManualCompany('');
    setManualWebsite('');
  };

  // `force` bypasses the server's short-TTL result cache — set by the
  // Retry button, since a deliberate re-audit wants fresh data rather
  // than a replay of the verdict that just failed or looked wrong.
  const handleAudit = async (index, force = false) => {
    const lead = leadsRef.current[index];
    setLeads(prev => {
      const newLeads = [...prev];
      newLeads[index].auditState = 'auditing';
      newLeads[index].auditProgress = null;
      return newLeads;
    });

    // An audit is a single long blocking POST (a couple of minutes now that
    // the tool timeouts were raised for accuracy), so poll the server for
    // which stage it's actually on rather than showing a bare spinner the
    // whole time.
    let pollTimer = null;
    if (lead.Website) {
      pollTimer = setInterval(async () => {
        try {
          const p = await axios.get(`${API_BASE}/api/audit/progress`, {
            params: { website: lead.Website }
          });
          if (!p.data?.running) return;
          setLeads(prev => {
            const updated = [...prev];
            if (updated[index]?.auditState === 'auditing') {
              updated[index].auditProgress = p.data;
            }
            return updated;
          });
        } catch {
          // Progress is cosmetic — never let a failed poll disturb the audit.
        }
      }, 1500);
    }

    try {
      const res = await axios.post(`${API_BASE}/api/audit`, {
        company: lead.Company,
        website: lead.Website,
        instagram_handle: lead['Instagram Handle'],
        // Google Business Profile fields the search already returned for this
        // lead. They drive the rating personalization hook and the NAP
        // consistency check (site details vs the Google listing) — both are
        // skipped server-side when empty, so a manually-added lead still works.
        rating: lead.Rating || '',
        reviews_count: Number(lead['Reviews Count']) || 0,
        gbp_phone: lead.Phone || '',
        // Manually-added leads carry the literal placeholder 'Added Manually'
        // in Address and have no Google listing at all — sending it would
        // feed a non-address into the NAP comparison.
        gbp_address: lead.Address === 'Added Manually' ? '' : (lead.Address || ''),
        force,
        sector: lead.sector || '',
        sector_detail: lead.sectorDetail || ''
      });

      setLeads(prev => {
        const updatedLeads = [...prev];
        if (res.data.error) {
          updatedLeads[index].auditState = 'failed';
        } else {
          updatedLeads[index].auditState = 'done';
          updatedLeads[index].auditData = res.data;
        }
        updatedLeads[index].auditProgress = null;
        return updatedLeads;
      });
    } catch (err) {
      console.error(`Audit failed for ${lead.Company}:`, err.response?.data?.detail || err.message, err);

      // /api/audit runs for a couple of minutes with nothing that cancels it
      // if the client's connection drops mid-request — the backend finishes
      // and saves a real draft regardless of whether this response ever
      // arrives. A network hiccup here can therefore look identical to a
      // real failure while a usable draft already exists; check for it
      // before showing "failed" and sending the human on a wasted, paid-for
      // retry of an audit that in fact already succeeded.
      let recovered = null;
      if (lead.Website) {
        try {
          const r = await axios.get(`${API_BASE}/api/audit/recover`, { params: { website: lead.Website } });
          recovered = r.data?.draft || null;
        } catch {
          // The recovery check itself failing just means "can't tell" —
          // fall through to the normal failed state below.
        }
      }

      setLeads(prev => {
        const updatedLeads = [...prev];
        if (recovered) {
          updatedLeads[index].auditState = 'done';
          updatedLeads[index].auditData = {
            email: recovered.target_email,
            subject: recovered.subject,
            body: recovered.body,
            image_url: recovered.image_url || null,
            review_warnings: recovered.review_warnings || [],
            // Not stored on the draft row, so left absent — the card
            // already renders these as "n/a" rather than a wrong 0.
            page_speed_score: null,
            seo_score: null,
            recoveredAfterDroppedConnection: true,
          };
        } else {
          updatedLeads[index].auditState = 'failed';
        }
        updatedLeads[index].auditProgress = null;
        return updatedLeads;
      });
    } finally {
      if (pollTimer) clearInterval(pollTimer);
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

  // A draft's audit data is frozen when it's generated, but the draft can sit
  // in this inbox for weeks — so the age is shown on the card, matching the
  // staleness gate /api/send enforces server-side (DRAFT_STALE_DAYS).
  const draftAgeDays = (draft) => {
    if (!draft.timestamp) return null;
    const drafted = new Date(draft.timestamp.replace(' ', 'T'));
    if (isNaN(drafted)) return null;
    return Math.floor((Date.now() - drafted.getTime()) / 86400000);
  };

  // /api/send returns 409 when the draft carries unacknowledged review
  // warnings or is old enough that its audit data may be stale. That's a
  // deliberate stop, not an error: show the human exactly what was flagged
  // and only retry if they explicitly choose to send anyway.
  const sendWithAcknowledgement = async (payload) => {
    try {
      return await axios.post(`${API_BASE}/api/send`, payload);
    } catch (err) {
      if (err.response?.status !== 409) throw err;

      const detail = err.response.data?.detail || {};
      const warnings = detail.warnings || [];
      const proceed = window.confirm(
        `${detail.message || 'This draft was flagged during review.'}\n\n` +
        warnings.map((w, i) => `${i + 1}. ${w}`).join('\n\n') +
        `\n\nSend it anyway?`
      );
      if (!proceed) return null;

      return await axios.post(`${API_BASE}/api/send`, { ...payload, acknowledge_warnings: true });
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
      const result = await sendWithAcknowledgement({
        email: lead.auditData.email,
        subject: lead.auditData.subject,
        body: lead.auditData.body,
        company: lead.Company,
        website: lead.Website,
        // Undefined until the human clicks "Remove image", so the default
        // stays "attach it" exactly as before.
        attach_screenshot: lead.auditData.attach_screenshot !== false
      });

      // null means the human saw the warnings and chose not to send.
      const finalLeads = [...leads];
      finalLeads[index].auditState = result ? 'sent' : 'done';
      setLeads(finalLeads);
    } catch (err) {
      console.error('Send failed:', err);
      alert(`Failed to send email: ${err.response?.data?.detail || err.message}`);
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
      const result = await sendWithAcknowledgement({
        email: draft.target_email,
        subject: draft.subject,
        body: draft.body,
        company: draft.company,
        website: draft.website,
        attach_screenshot: draft.attach_screenshot !== false
      });

      if (!result) {
        // Human declined after seeing the warnings — leave the draft in place.
        setDrafts(originalDrafts);
        return;
      }

      // Remove from drafts list since it was sent
      setDrafts(drafts.filter(d => d.id !== draft.id));
    } catch (err) {
      console.error('Draft send failed:', err);
      alert(`Failed to send draft email: ${err.response?.data?.detail || err.message}`);
      setDrafts(originalDrafts);
    }
  };

  const handleDraftDelete = async (draftId) => {
    try {
      await axios.delete(`${API_BASE}/api/drafts/${draftId}`);
      setDrafts(drafts.filter(d => d.id !== draftId));
    } catch (err) {
      console.error('Draft delete failed:', err);
      alert(`Failed to delete draft: ${err.response?.data?.detail || err.message}`);
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
            {NICHES.map(n => <option key={n} value={n} />)}
          </datalist>
        </div>
        <div className="input-group">
          <label>City</label>
          <input type="text" list="city-options" value={city} onChange={e => setCity(e.target.value)} placeholder="e.g. Mumbai" required />
          <datalist id="city-options">
            {CITIES.map(c => <option key={c} value={c} />)}
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
          <button type="button" onClick={() => setShowManualEntry(!showManualEntry)} style={{ background: showManualEntry ? '#fee2e2' : '#f8fafc', border: showManualEntry ? '1px solid #f87171' : '1px solid #cbd5e1', color: showManualEntry ? '#ef4444' : '#334155', padding: '0 20px', borderRadius: '12px', cursor: 'pointer', height: '48px', fontSize: '15px', fontWeight: 'bold', transition: 'all 0.2s', whiteSpace: 'nowrap', boxShadow: '0 2px 4px rgba(0,0,0,0.05)' }}>
            {showManualEntry ? 'Cancel' : '+ Specific Lead'}
          </button>
        </div>
      </form>

      {/* Nearby search is deliberately outside the form above: it needs
          neither niche nor city (both `required` there), and submitting the
          form would fail validation before this could ever run. */}
      <div className="search-box glass" style={{ marginTop: 16, alignItems: 'flex-end' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <label style={{ display: 'block', fontWeight: 600, marginBottom: 6, color: '#334155' }}>
            Or find every type of business near you
          </label>
          <p style={{ margin: 0, fontSize: 13, color: '#64748b' }}>
            Uses your device location and searches across all industries, no niche needed.
          </p>
        </div>
        <div className="input-group" style={{ maxWidth: 140 }}>
          <label>Radius (km)</label>
          <input
            type="number" min="1" max="50" value={nearbyRadiusKm}
            onChange={e => setNearbyRadiusKm(e.target.value)}
          />
        </div>
        <button
          type="button" onClick={handleSearchNearby} disabled={loadingNearby}
          className="primary-btn"
          style={{ background: loadingNearby ? '#94a3b8' : '#0f766e' }}
        >
          {loadingNearby ? <Loader2 className="spin" /> : <MapPin />}
          {loadingNearby ? 'Searching...' : 'Find Leads Near Me'}
        </button>
      </div>

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
            style={{ background: isAutopilot ? '#ef4444' : '#10b981', color: '#ffffff', border: 'none', padding: '12px 24px', fontSize: '16px', fontWeight: 'bold', borderRadius: '8px', boxShadow: '0 4px 6px rgba(0,0,0,0.1)' }}
          >
            <Activity className={isAutopilot ? 'spin' : ''} />
            {isAutopilot ? 'Stop Autopilot' : 'Start Autopilot (Audit All)'}
          </button>
        </div>
      )}

      <div className="leads-grid">
        <AnimatePresence>
          {leads.slice((leadsPage - 1) * LEADS_PAGE_SIZE, leadsPage * LEADS_PAGE_SIZE).map((lead, pageI) => {
          const i = (leadsPage - 1) * LEADS_PAGE_SIZE + pageI;
          return (
            <motion.div key={i} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: 0.9 }} className={`lead-card glass ${lead.auditState === 'rejected' ? 'rejected' : ''}`}>
              <div className="lead-header">
                <h3>{lead.Company}</h3>
                {lead.auditState === 'sent' && <span className="badge success"><Check size={14}/> Sent</span>}
                {lead.auditState === 'rejected' && <span className="badge danger"><X size={14}/> Rejected</span>}
              </div>
              
              <div className="lead-details">
                <p><strong>URL:</strong> <a href={lead.Website} target="_blank" rel="noreferrer">{lead.Website || 'N/A'}</a></p>
                <p><strong>Address:</strong> {lead.Address}</p>
                {/* Only nearby searches set this — a niche search already
                    tells you the category, but an all-types search doesn't. */}
                {lead.Category && (
                  <p style={{ margin: '4px 0 0' }}>
                    <span style={{ display: 'inline-block', padding: '2px 10px', borderRadius: 999, background: '#ccfbf1', color: '#0f766e', fontSize: 12, fontWeight: 600 }}>
                      {lead.Category}
                    </span>
                  </p>
                )}
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

              {!lead.Website && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <p className="error-text">Cannot audit — no website found.</p>
                  {lead.Phone && (
                    <a
                      href={`https://wa.me/${phoneToWhatsAppDigits(lead.Phone)}?text=${encodeURIComponent(`Hi, I help businesses like ${lead.Company} get found online — mind if I share a couple of quick ideas?`)}`}
                      target="_blank" rel="noreferrer"
                      style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: '#25D366', color: '#fff', borderRadius: '8px', textDecoration: 'none', fontWeight: 'bold', fontSize: '14px', width: 'fit-content' }}
                    >
                      <MessageCircle size={16} /> Message on WhatsApp
                    </a>
                  )}
                </div>
              )}
              {lead.auditState === 'auditing' && (
                <div className="auditing-state" style={{ flexDirection: 'column', gap: '10px' }}>
                  <Loader2 className="spin" size={24} />
                  {lead.auditProgress ? (
                    <>
                      <p style={{ margin: 0 }}>{lead.auditProgress.stage}</p>
                      <div style={{ width: '100%', maxWidth: '320px' }}>
                        <div style={{ height: '6px', background: '#e2e8f0', borderRadius: '999px', overflow: 'hidden' }}>
                          <div style={{
                            height: '100%',
                            width: `${((lead.auditProgress.stage_index + 1) / lead.auditProgress.total_stages) * 100}%`,
                            background: '#3b82f6',
                            borderRadius: '999px',
                            transition: 'width 0.4s ease'
                          }} />
                        </div>
                        <p style={{ fontSize: '12px', color: '#64748b', margin: '6px 0 0', textAlign: 'center' }}>
                          Step {lead.auditProgress.stage_index + 1} of {lead.auditProgress.total_stages}
                        </p>
                      </div>
                    </>
                  ) : (
                    <p style={{ margin: 0 }}>Running analysis...</p>
                  )}
                </div>
              )}
              {lead.auditState === 'failed' && (
                <div className="auditing-state" style={{ flexDirection: 'column', gap: '8px' }}>
                  <p className="error-text">Audit failed.</p>
                  <button className="audit-btn" onClick={() => handleAudit(i, true)}><Activity size={18} /> Retry Audit</button>
                </div>
              )}
              {lead.auditState === 'sending' && <div className="auditing-state"><Loader2 className="spin" size={24} /><p>Sending via SES...</p></div>}

              {lead.auditState === 'done' && lead.auditData && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} className="audit-results">
                  {lead.auditData.recoveredAfterDroppedConnection && (
                    <div className="recovered-note">
                      The connection dropped while this audit was running, but it finished on the
                      server and this draft is real — recovered automatically. Speed/SEO scores
                      aren't stored with the draft, so they show as n/a here; re-run the audit if
                      you need them.
                    </div>
                  )}
                  <div className="stats-row">
                    {/* A 0 here means the measurement tool failed, not that the
                        site genuinely scored zero — showing "0/100" made a
                        failed measurement look like a catastrophic result. */}
                    <div className="stat-box"><Zap size={16} /><span>Speed</span><strong>{lead.auditData.page_speed_score ? `${lead.auditData.page_speed_score}/100` : 'n/a'}</strong></div>
                    <div className="stat-box"><BarChart size={16} /><span>SEO</span><strong>{lead.auditData.seo_score ? `${lead.auditData.seo_score}/100` : 'n/a'}</strong></div>
                  </div>
                  {lead.auditData.budget_signal && (
                    <div
                      className={`budget-badge budget-badge--${lead.auditData.budget_signal.tier}`}
                      title={lead.auditData.budget_signal.signals.join(' · ') || 'No specific signals detected'}
                    >
                      <DollarSign size={14} />
                      <span>{lead.auditData.budget_signal.label}</span>
                    </div>
                  )}
                  {lead.auditData.signal_status && Object.values(lead.auditData.signal_status).some(s => s !== 'ok') && (
                    <div style={{
                      background: '#fffbeb',
                      border: '1px solid #fde68a',
                      borderRadius: '8px',
                      padding: '10px 12px',
                      margin: '0 0 12px',
                      fontSize: '13px',
                      color: '#92400e'
                    }}>
                      <strong>Partial coverage:</strong>{' '}
                      {Object.entries(lead.auditData.signal_status)
                        .filter(([, s]) => s !== 'ok')
                        .map(([name]) => name.replace(/_/g, ' '))
                        .join(', ')}{' '}
                      returned no data. Flaws in those areas could not be detected on this run, so their absence does not mean the site is clean.
                    </div>
                  )}
                  <div className="email-draft">
                    <h4><FileText size={16} /> Drafted Email</h4>
                    {lead.auditData.image_url && lead.auditData.attach_screenshot !== false && (
                      <div style={{ marginBottom: '16px', textAlign: 'center' }}>
                        <img src={lead.auditData.image_url} alt="Website Screenshot" style={{ maxWidth: '100%', maxHeight: '400px', borderRadius: '8px', border: '2px solid #ef4444' }} />
                        <button
                          className="remove-image-btn"
                          onClick={() => {
                            const newLeads = [...leads];
                            newLeads[i].auditData.attach_screenshot = false;
                            setLeads(newLeads);
                          }}
                        ><X size={14} /> Remove image</button>
                      </div>
                    )}
                    {lead.auditData.image_url && lead.auditData.attach_screenshot === false && (
                      <div className="image-removed-note">
                        Screenshot will not be attached.{' '}
                        <button onClick={() => {
                          const newLeads = [...leads];
                          newLeads[i].auditData.attach_screenshot = true;
                          setLeads(newLeads);
                        }}>Put it back</button>
                      </div>
                    )}
                    <p className="target-email"><strong>To:</strong> {lead.auditData.email || 'Email not found (will fail)'}</p>
                    <label className="subject-label">Subject</label>
                    <input
                      className="subject-editor"
                      value={lead.auditData.subject || ''}
                      onChange={(e) => {
                        const newLeads = [...leads];
                        newLeads[i].auditData.subject = e.target.value;
                        setLeads(newLeads);
                      }}
                    />
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
                    {lead.Phone && (
                      <a
                        href={`https://wa.me/${phoneToWhatsAppDigits(lead.Phone)}?text=${encodeURIComponent(`${lead.auditData.subject}\n\n${lead.auditData.body}`)}`}
                        target="_blank" rel="noreferrer"
                        style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', padding: '0 20px', height: '48px', background: '#25D366', color: '#fff', borderRadius: '8px', textDecoration: 'none', fontWeight: 'bold', fontSize: '15px' }}
                      >
                        <MessageCircle size={18} /> Send via WhatsApp
                      </a>
                    )}
                    <button className="send-btn" onClick={() => handleSend(i)}><Send size={18} /> Approve & Send</button>
                  </div>
                </motion.div>
              )}
            </motion.div>
          );})}
        </AnimatePresence>
      </div>

      {leads.length > LEADS_PAGE_SIZE && (
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '16px', margin: '24px 0' }}>
          <button
            onClick={() => setLeadsPage(p => Math.max(1, p - 1))}
            disabled={leadsPage === 1}
            style={{ padding: '8px 16px', borderRadius: '8px', border: '1px solid #cbd5e1', background: '#f8fafc', cursor: leadsPage === 1 ? 'not-allowed' : 'pointer', opacity: leadsPage === 1 ? 0.5 : 1 }}
          >
            Previous
          </button>
          <span style={{ color: '#94a3b8', fontSize: '14px' }}>
            Page {leadsPage} of {Math.ceil(leads.length / LEADS_PAGE_SIZE)}
          </span>
          <button
            onClick={() => setLeadsPage(p => Math.min(Math.ceil(leads.length / LEADS_PAGE_SIZE), p + 1))}
            disabled={leadsPage >= Math.ceil(leads.length / LEADS_PAGE_SIZE)}
            style={{ padding: '8px 16px', borderRadius: '8px', border: '1px solid #cbd5e1', background: '#f8fafc', cursor: leadsPage >= Math.ceil(leads.length / LEADS_PAGE_SIZE) ? 'not-allowed' : 'pointer', opacity: leadsPage >= Math.ceil(leads.length / LEADS_PAGE_SIZE) ? 0.5 : 1 }}
          >
            Next
          </button>
        </div>
      )}
    </>
  );

  const renderAgriculture = () => (
    <>
      <header className="header">
        <p className="subtitle">Agriculture sector — preset niches, click to search</p>
      </header>

      {agriLastResult && (
        <div style={{ background: 'rgba(16, 185, 129, 0.1)', border: '1px solid rgba(16, 185, 129, 0.3)', borderRadius: 8, padding: '10px 16px', marginBottom: 16, color: '#10b981', fontSize: 14, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <span>{agriLastResult}</span>
          <button type="button" onClick={() => setCurrentView('home')} style={{ background: 'transparent', border: '1px solid #10b981', color: '#10b981', borderRadius: 6, padding: '4px 12px', cursor: 'pointer', fontSize: 13, whiteSpace: 'nowrap' }}>
            Go to Dashboard
          </button>
        </div>
      )}

      <div className="search-box glass" style={{ marginBottom: 16 }}>
        <div className="input-group">
          <label>City</label>
          <input type="text" list="city-options" value={agriCity} onChange={e => setAgriCity(e.target.value)} placeholder="e.g. Nashik" />
          <datalist id="city-options">
            {CITIES.map(c => <option key={c} value={c} />)}
          </datalist>
        </div>
        <div className="input-group" style={{ maxWidth: '100px' }}>
          <label>Leads</label>
          <input type="number" value={limit} onChange={e => setLimit(e.target.value)} min="1" max="100" />
        </div>
        <div className="input-group" style={{ maxWidth: '180px' }}>
          <label>Directory</label>
          <select value={b2bDirectory} onChange={e => setB2bDirectory(e.target.value)}>
            <option value="indiamart">IndiaMART</option>
            <option value="tradeindia">TradeIndia</option>
            <option value="exportersindia">ExportersIndia</option>
            <option value="all">All Directories</option>
          </select>
        </div>
      </div>

      <p style={{ color: '#94a3b8', fontSize: 13, margin: '0 0 16px' }}>
        Google Maps skips listings with no website, and a lot of agri dealers/farms
        don't have one — so also try a B2B directory (pick one above), which surfaces
        suppliers whether or not they have a site (those come through with no
        Website, but still a phone for WhatsApp outreach).
      </p>

      <div className="search-box glass" style={{ flexDirection: 'column', alignItems: 'stretch', gap: '10px', padding: '16px', marginBottom: 20 }}>
        <strong style={{ color: '#e2e8f0', fontSize: 14 }}>Maharashtra's official licensed seed-dealer list</strong>
        <p style={{ color: '#94a3b8', fontSize: 13, margin: 0 }}>
          Real government data (krishi.maharashtra.gov.in) — comes with name, phone
          and email already filled in, no guessing. City above optionally narrows by
          district/taluka; leave it blank to pull from the whole list.
        </p>
        <button
          type="button" className="primary-btn" style={{ width: 'fit-content', fontSize: 13, padding: '8px 16px', background: '#0f766e' }}
          disabled={!!agriSearchKey || agriBulkRunning} onClick={handleKrishiMaharashtraSearch}
        >
          {agriSearchKey === 'krishi-maharashtra' ? <Loader2 className="spin" size={14} /> : <Search size={14} />} Search licensed dealers
        </button>
      </div>

      <div className="search-box glass" style={{ flexDirection: 'column', alignItems: 'stretch', gap: '10px', padding: '16px', marginBottom: 20, borderColor: 'rgba(59, 130, 246, 0.3)' }}>
        <strong style={{ color: '#e2e8f0', fontSize: 14 }}>Search every niche at once</strong>
        <p style={{ color: '#94a3b8', fontSize: 13, margin: 0 }}>
          Runs Maps + the directory selected above (or all three, if "All Directories" is
          picked) across all {AGRI_NICHES.length} niches, one request at a time so it stays
          well under the API's rate limits. Slow on purpose — expect several minutes; leads
          land in your list as each one finishes, so you don't have to wait for the end.
        </p>
        {agriBulkRunning ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Loader2 className="spin" size={16} />
              <span style={{ color: '#e2e8f0', fontSize: 13 }}>
                {agriBulkProgress ? `${agriBulkProgress.current}/${agriBulkProgress.total} — ${agriBulkProgress.label}` : 'Starting...'}
              </span>
            </div>
            <div style={{ width: '100%', maxWidth: 320, height: 6, background: '#e2e8f0', borderRadius: 999, overflow: 'hidden' }}>
              <div style={{
                height: '100%',
                width: agriBulkProgress ? `${(agriBulkProgress.current / agriBulkProgress.total) * 100}%` : '0%',
                background: '#3b82f6', borderRadius: 999, transition: 'width 0.4s ease',
              }} />
            </div>
            <button
              type="button" onClick={() => { agriBulkStopRef.current = true; }}
              style={{ width: 'fit-content', background: '#fee2e2', border: '1px solid #f87171', color: '#ef4444', padding: '6px 14px', borderRadius: 8, cursor: 'pointer', fontSize: 13, fontWeight: 'bold' }}
            >
              Stop
            </button>
          </div>
        ) : (
          <button
            type="button" className="primary-btn" style={{ width: 'fit-content', fontSize: 13, padding: '8px 16px', background: '#3b82f6' }}
            disabled={!!agriSearchKey} onClick={handleSearchAllNiches}
          >
            <Search size={14} /> Search All Niches
          </button>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '12px' }}>
        {AGRI_NICHES.map(n => {
          const mapsKey = `${n}|maps`;
          const directoryKey = `${n}|directory`;
          const directoryLabel = { indiamart: 'IndiaMART', tradeindia: 'TradeIndia', exportersindia: 'ExportersIndia', all: 'All Directories' }[b2bDirectory];
          const anyBusy = !!agriSearchKey || agriBulkRunning;
          return (
            <div key={n} className="search-box glass" style={{ flexDirection: 'column', alignItems: 'stretch', gap: '10px', padding: '16px' }}>
              <strong style={{ color: '#e2e8f0', fontSize: 14 }}>{n}</strong>
              <div style={{ display: 'flex', gap: '8px' }}>
                <button
                  type="button" className="primary-btn" style={{ flex: 1, fontSize: 13, padding: '8px 12px' }}
                  disabled={anyBusy} onClick={() => handleAgriSearch(n, 'maps')}
                >
                  {agriSearchKey === mapsKey ? <Loader2 className="spin" size={14} /> : <Search size={14} />} Maps
                </button>
                <button
                  type="button" className="primary-btn" style={{ flex: 1, fontSize: 13, padding: '8px 12px', background: '#0f766e' }}
                  disabled={anyBusy} onClick={() => handleAgriSearch(n, 'directory')}
                >
                  {agriSearchKey === directoryKey ? <Loader2 className="spin" size={14} /> : <Search size={14} />} {directoryLabel}
                </button>
              </div>
            </div>
          );
        })}
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

              {draftAgeDays(draft) !== null && draftAgeDays(draft) >= 7 && (
                <div style={{
                  marginTop: '12px', padding: '10px 12px', borderRadius: '8px',
                  background: 'rgba(245, 158, 11, 0.12)', border: '1px solid rgba(245, 158, 11, 0.4)',
                  color: '#fcd34d', fontSize: '13px',
                  display: 'flex', alignItems: 'center', gap: '6px',
                }}>
                  <AlertTriangle size={14} />
                  Drafted {draftAgeDays(draft)} days ago — the site may have changed since this was audited.
                </div>
              )}

              {draft.review_warnings && draft.review_warnings.length > 0 && (
                <div style={{
                  marginTop: '12px', padding: '10px 12px', borderRadius: '8px',
                  background: 'rgba(239, 68, 68, 0.12)', border: '1px solid rgba(239, 68, 68, 0.4)',
                  color: '#fca5a5', fontSize: '13px',
                }}>
                  <div style={{display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600, marginBottom: '6px'}}>
                    <AlertTriangle size={14} /> Review before sending ({draft.review_warnings.length})
                  </div>
                  <ul style={{margin: 0, paddingLeft: '18px'}}>
                    {draft.review_warnings.map((w, wi) => <li key={wi}>{w}</li>)}
                  </ul>
                </div>
              )}

              {draft.sending ? (
                <div className="auditing-state"><Loader2 className="spin" size={24} /><p>Sending via SES...</p></div>
              ) : (
                <div className="email-draft" style={{ marginTop: '16px' }}>
                  {draft.image_url && draft.attach_screenshot !== false && (
                    <div style={{ marginBottom: '16px', textAlign: 'center' }}>
                      <img src={draft.image_url} alt="Website Screenshot" style={{ maxWidth: '100%', maxHeight: '400px', borderRadius: '8px', border: '2px solid #ef4444' }} />
                      <button
                        className="remove-image-btn"
                        onClick={() => {
                          const newDrafts = [...drafts];
                          newDrafts[i] = { ...draft, attach_screenshot: false };
                          setDrafts(newDrafts);
                        }}
                      ><X size={14} /> Remove image</button>
                    </div>
                  )}
                  {draft.image_url && draft.attach_screenshot === false && (
                    <div className="image-removed-note">
                      Screenshot will not be attached.{' '}
                      <button onClick={() => {
                        const newDrafts = [...drafts];
                        newDrafts[i] = { ...draft, attach_screenshot: true };
                        setDrafts(newDrafts);
                      }}>Put it back</button>
                    </div>
                  )}
                  <label className="subject-label">Subject</label>
                  <input
                    className="subject-editor"
                    value={draft.subject || ''}
                    onChange={(e) => {
                      const newDrafts = [...drafts];
                      newDrafts[i] = { ...draft, subject: e.target.value };
                      setDrafts(newDrafts);
                    }}
                  />
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

  const handleCheckReplies = async () => {
    setCheckingReplies(true);
    try {
      const res = await axios.post(`${API_BASE}/api/check-replies`);
      const s = res.data;
      alert(
        `Scanned ${s.scanned} messages.\n\n` +
        `${s.replies} real replies\n${s.auto_replies} auto-replies (out of office)\n` +
        `${s.bounces} bounces\n${s.unmatched} unrelated to anything we sent`
      );
      const res2 = await axios.get(`${API_BASE}/api/history?t=${Date.now()}`);
      setHistoryLogs(res2.data.history);
    } catch (err) {
      console.error(err);
      alert(err.response?.data?.detail || 'Could not check replies.');
    } finally {
      setCheckingReplies(false);
    }
  };

  const renderHistory = () => (
    <div className="glass" style={{ padding: '24px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
        <h2 style={{ margin: 0 }}><Clock style={{display:'inline', marginRight: '8px', verticalAlign: 'middle'}}/> Email Sent History</h2>
        {replyCheckEnabled && (
          <button
            onClick={handleCheckReplies}
            disabled={checkingReplies}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: checkingReplies ? 'rgba(148,163,184,0.2)' : '#10b981', border: 'none', borderRadius: '8px', color: '#fff', cursor: checkingReplies ? 'default' : 'pointer', fontSize: '14px', fontWeight: 'bold' }}
          >
            {checkingReplies ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
            {checkingReplies ? 'Checking inbox...' : 'Check for replies'}
          </button>
        )}
      </div>
      <p style={{color: '#94a3b8', marginBottom: '16px', marginTop: '8px'}}>Persistent log of all outbound emails dispatched.</p>

      {variantPerf.length > 0 && (
        <div style={{ background: 'rgba(148,163,184,0.08)', border: '1px solid rgba(148,163,184,0.25)', borderRadius: '8px', padding: '14px 16px', marginBottom: '16px' }}>
          <div style={{ fontWeight: 600, marginBottom: '4px' }}>Copy performance by variant</div>
          <div style={{ color: '#94a3b8', fontSize: '12px', marginBottom: '10px' }}>
            Currently sending: <strong>{currentVariant || 'unset'}</strong>. Reply rate is the number that matters &mdash;
            open rate is distorted by image pre-fetching and images-off readers.
          </div>
          <table style={{ width: '100%', fontSize: '13px', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ color: '#94a3b8', textAlign: 'left' }}>
                <th style={{ padding: '4px 8px 4px 0' }}>Variant</th>
                <th style={{ padding: '4px 8px' }}>Sent</th>
                <th style={{ padding: '4px 8px' }}>Replied</th>
                <th style={{ padding: '4px 8px' }}>Reply rate</th>
                <th style={{ padding: '4px 8px' }}>Open rate</th>
                <th style={{ padding: '4px 8px' }}>Avg words</th>
              </tr>
            </thead>
            <tbody>
              {variantPerf.map(v => (
                <tr key={v.variant} style={{ borderTop: '1px solid rgba(148,163,184,0.15)' }}>
                  <td style={{ padding: '6px 8px 6px 0' }}>{v.variant}</td>
                  <td style={{ padding: '6px 8px' }}>{v.sent}</td>
                  <td style={{ padding: '6px 8px' }}>{v.replied}</td>
                  <td style={{ padding: '6px 8px', fontWeight: 600 }}>
                    {v.enough_data
                      ? `${v.reply_rate}%`
                      : <span style={{ color: '#fcd34d', fontWeight: 400 }}>too few sends to tell</span>}
                  </td>
                  <td style={{ padding: '6px 8px', color: '#94a3b8' }}>{v.enough_data ? `${v.open_rate}%` : '—'}</td>
                  <td style={{ padding: '6px 8px', color: '#94a3b8' }}>{v.avg_words || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!replyCheckEnabled && (
        <div style={{ background: 'rgba(148,163,184,0.08)', border: '1px solid rgba(148,163,184,0.25)', borderRadius: '8px', padding: '12px 16px', marginBottom: '16px', fontSize: '13px', color: '#94a3b8' }}>
          Reply detection is off. Set <code>IMAP_USER</code> and <code>IMAP_PASSWORD</code> (an App Password)
          to see who actually wrote back — the one engagement signal that is never wrong.
        </div>
      )}

      {replyCheckEnabled && (
        <div style={{ background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.3)', borderRadius: '8px', padding: '12px 16px', marginBottom: '16px', fontSize: '13px', color: '#94a3b8' }}>
          <strong style={{ color: '#22c55e' }}>
            {historyLogs.filter(l => l.replied).length} replies
          </strong>
          {' '}from {historyLogs.length} emails sent
          {historyLogs.filter(l => l.bounced).length > 0 && (
            <span style={{ color: '#ef4444' }}> · {historyLogs.filter(l => l.bounced).length} bounced</span>
          )}
          . Replies are exact — unlike opens, nothing inflates or hides them.
        </div>
      )}

      {trackingEnabled ? (
        <div style={{ background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.3)', borderRadius: '8px', padding: '12px 16px', marginBottom: '24px', fontSize: '13px', color: '#94a3b8' }}>
          <strong style={{ color: '#10b981' }}>
            {historyLogs.filter(l => l.open_count > 0).length} of {historyLogs.length} opened
          </strong>
          {' '}— counts are approximate. Apple Mail loads images automatically, so some
          &ldquo;opens&rdquo; are nobody; a reader with images off shows as never opened.
          Treat it as a trend, not a headcount.
        </div>
      ) : (
        <div style={{ background: 'rgba(148,163,184,0.08)', border: '1px solid rgba(148,163,184,0.25)', borderRadius: '8px', padding: '12px 16px', marginBottom: '24px', fontSize: '13px', color: '#94a3b8' }}>
          Open tracking is off, so every email below shows as unopened regardless of what
          actually happened. Set <code>EMAIL_OPEN_TRACKING=true</code> and <code>APP_BASE_URL</code> to turn it on.
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {historyLogs.map(log => (
          <div key={log.id} style={{ background: 'rgba(255,255,255,0.02)', padding: '16px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <h4 style={{ margin: '0 0 4px 0' }}>{log.company}</h4>
                <p style={{ margin: 0, fontSize: '13px', color: '#64748b' }}>To: {log.target_email} • From: {log.sender_email}</p>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
                {log.replied && (
                  <span title={log.reply_subject ? `Re: ${log.reply_subject}` : 'Replied'} style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', marginTop: '8px', fontSize: '12px', fontWeight: 'bold', color: '#22c55e', background: 'rgba(34,197,94,0.15)', border: '1px solid rgba(34,197,94,0.4)', borderRadius: '999px', padding: '3px 10px' }}>
                    <MessageSquare size={13} /> Replied
                  </span>
                )}
                {log.bounced && (
                  <span title="Delivery failed — this address may be dead" style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', marginTop: '8px', fontSize: '12px', fontWeight: 'bold', color: '#ef4444', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)', borderRadius: '999px', padding: '3px 10px' }}>
                    <AlertTriangle size={13} /> Bounced
                  </span>
                )}
                {log.auto_replied && !log.replied && (
                  <span title="Automatic response — proves the address is live, not that anyone read it" style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', marginTop: '8px', fontSize: '12px', color: '#94a3b8', background: 'rgba(148,163,184,0.1)', border: '1px solid rgba(148,163,184,0.25)', borderRadius: '999px', padding: '3px 10px' }}>
                    <MessageSquare size={13} /> Auto-reply
                  </span>
                )}
                {trackingEnabled && (
                  log.open_count > 0 ? (
                    <span title={`First opened ${log.first_opened_at} UTC`} style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', marginTop: '8px', fontSize: '12px', fontWeight: 'bold', color: '#10b981', background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.3)', borderRadius: '999px', padding: '3px 10px' }}>
                      <Eye size={13} /> Opened{log.open_count > 1 ? ` ${log.open_count}x` : ''}
                    </span>
                  ) : (
                    <span title={log.automated_count > 0 ? `${log.automated_count} fetch(es), all from scanners or bots — not a person` : 'No pixel fetch recorded'} style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', marginTop: '8px', fontSize: '12px', color: '#64748b', background: 'rgba(148,163,184,0.08)', border: '1px solid rgba(148,163,184,0.2)', borderRadius: '999px', padding: '3px 10px' }}>
                      <EyeOff size={13} /> {log.automated_count > 0 ? 'Scanner only' : 'Not opened'}
                    </span>
                  )
                )}
                </div>
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
            onClick={() => setCurrentView('agriculture')}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: currentView === 'agriculture' ? 'rgba(255,255,255,0.15)' : 'transparent', border: 'none', borderRadius: '8px', color: currentView === 'agriculture' ? '#fff' : '#94a3b8', cursor: 'pointer', fontSize: '15px', fontWeight: currentView === 'agriculture' ? 'bold' : 'normal', transition: 'all 0.2s' }}
          >
            <Sprout size={18} /> Agriculture
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

          <button
            type="button"
            onClick={() => setShowHelp(v => !v)}
            title="How to use this page"
            style={{ marginLeft: '12px', width: 34, height: 34, borderRadius: '50%', background: showHelp ? 'rgba(255,255,255,0.15)' : 'transparent', border: '1px solid rgba(255,255,255,0.2)', color: '#e2e8f0', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            <HelpCircle size={18} />
          </button>
        </div>
      </nav>

      {showHelp && HELP_CONTENT[currentView] && (
        <>
          <div onClick={() => setShowHelp(false)} style={{ position: 'fixed', inset: 0, zIndex: 19 }} />
          <div style={{
            position: 'fixed', top: 92, right: '4%', maxWidth: 420, width: '92%',
            background: 'rgba(15, 23, 42, 0.97)', border: '1px solid rgba(255,255,255,0.15)',
            borderRadius: 16, padding: '20px 22px', boxShadow: '0 12px 40px rgba(0,0,0,0.4)', zIndex: 20,
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
              <h3 style={{ margin: 0, color: '#fff', fontSize: 16 }}>{HELP_CONTENT[currentView].title}</h3>
              <button type="button" onClick={() => setShowHelp(false)} style={{ background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer', padding: 0 }}>
                <X size={18} />
              </button>
            </div>
            <ul style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 10 }}>
              {HELP_CONTENT[currentView].tips.map((tip, i) => (
                <li key={i} style={{ color: '#cbd5e1', fontSize: 13.5, lineHeight: 1.5 }}>{tip}</li>
              ))}
            </ul>
          </div>
        </>
      )}

      {/* Main Content */}
      <main className="main-content" style={{ flex: 1, padding: '40px', overflowY: 'auto' }}>
        {currentView === 'home' && renderHome()}
        {currentView === 'agriculture' && renderAgriculture()}
        {currentView === 'drafts' && renderDrafts()}
        {currentView === 'history' && renderHistory()}
        {currentView === 'cost' && renderCost()}
      </main>
    </div>
  );
}

export default App;
