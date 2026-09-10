import { useState, useRef, useEffect, useMemo } from 'react';
import axios from 'axios';
import { Search, Zap, Send, Loader2, X, Check, Activity, BarChart, FileText, Home, Clock, DollarSign, LayoutDashboard, Calendar, FileEdit, MapPin, Eye, EyeOff, MessageSquare, MessageCircle, AlertTriangle, RefreshCw, Sprout, Shirt, HelpCircle } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { NICHES, CITIES } from './searchOptions';
import { AGRI_NICHES } from './agriNiches';
import { MNC_NAMES } from './mncNames';
import './App.css';

// Strips everything but digits, since wa.me only accepts a bare
// countrycode+number — the "+91 98765 43210" GBP format doesn't work as-is.
function phoneToWhatsAppDigits(phone) {
  return (phone || '').replace(/\D/g, '');
}

// Matches app.py/db.py's normalisation for /api/sent-websites keys, so a
// lead re-scraped later (different scheme, a stray "www.", a trailing
// slash) still matches the website it was actually emailed at.
function normaliseWebsiteKey(url) {
  if (!url) return '';
  return url.trim().toLowerCase()
    .replace(/\/+$/, '')
    .replace(/^https?:\/\//, '')
    .replace(/^www\./, '');
}

// "Bigger client" comparators for the leads-grid "Sort by" control. Higher
// score sorts first for every key. A lead that hasn't been audited yet has
// no auditData.budget_signal at all, so 'tier'/'capital' treat it as the
// lowest possible value rather than crashing or sorting it arbitrarily — it
// isn't confirmed small, it's just unmeasured, but for THIS purpose (find
// the biggest ones first) unmeasured has to sort after measured-and-
// confirmed-established either way. Module-level (not inside App()) since
// neither depends on component state — keeps the sort useMemo below from
// needing a fresh reference on every render.
const LEAD_SORT_TIER_RANK = { established: 2, growing: 1, unclear: 0 };
const LEAD_SORT_COMPARATORS = {
  default: null,
  reviews: (lead) => Number(lead['Reviews Count']) || 0,
  rating: (lead) => {
    const r = parseFloat(lead.Rating);
    return Number.isFinite(r) ? r : 0;
  },
  capital: (lead) => lead.auditData?.budget_signal?.paidup_capital || 0,
  tier: (lead) => LEAD_SORT_TIER_RANK[lead.auditData?.budget_signal?.tier] ?? -1,
};

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
      'Type a state name instead of a city (e.g. "Maharashtra") to search the whole state in one go, not just one city.',
      'Use "Sort by" to bring bigger clients to the top — Most reviews/Highest rating work on any fresh search; Budget tier/Highest MCA capital only have a value once you\'ve audited a lead.',
    ],
  },
  agriculture: {
    title: 'Agriculture — targeting the agriculture sector specifically',
    tips: [
      'Set a city first — Maps and directory searches both need one (the government dealer list is the one exception; leave city blank there to pull from all of Maharashtra). A state name works too, for a Maps/directory search across the whole state.',
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
  // Real stage text for an in-flight async_mode /api/search call — see
  // handleSearch. Without this the "Scraping..." button showed nothing else
  // while a fallen-back-to-the-free-scraper search sat queued behind an
  // in-progress audit's Playwright lock for minutes (live-reported
  // 2026-09-08); this surfaces that specific wait instead of looking hung.
  const [searchProgressNote, setSearchProgressNote] = useState('');
  // "Search for MNCs" — a targeted lookup by company name (requested:
  // "add an option to specifically search for mncs") rather than a niche
  // search, since large/multinational businesses aren't found by category
  // the way "dentist" or "salon" are. Reuses /api/search as-is (Places Text
  // Search already accepts a free-text query, so a company name works
  // today) with limit=1 per name — you want THE listing for that company,
  // not 20 similar ones.
  const [showMncSearch, setShowMncSearch] = useState(false);
  const [mncNames, setMncNames] = useState('');
  const [mncCity, setMncCity] = useState('');
  const [mncSearching, setMncSearching] = useState(false);
  const [mncProgress, setMncProgress] = useState(null);
  const [mncLastResult, setMncLastResult] = useState('');
  // "Search Local Businesses" — same auto-search-by-location mechanism as
  // MNC search above, but for small/medium/local businesses (requested
  // 2026-09-08: "can i add for small medium and local brands too like
  // this"). Unlike MNC search there's no need for a new curated list — this
  // tool already ships one (NICHES, searchOptions.js, ~130 categories
  // spanning health, fitness, food, retail, trades, professional services
  // etc.), built specifically around businesses this pipeline audits well.
  // Runs the full list by default (confirmed over a smaller subset), so a
  // real run is long (30+ minutes at the 13s/request pacing below) —
  // stoppable mid-run like the MNC/Agriculture bulk runners.
  const [showLocalSearch, setShowLocalSearch] = useState(false);
  const [localNiches, setLocalNiches] = useState('');
  const [localCity, setLocalCity] = useState('');
  const [localSearching, setLocalSearching] = useState(false);
  const [localProgress, setLocalProgress] = useState(null);
  const [localLastResult, setLocalLastResult] = useState('');
  // "Search One Niche Across Many Cities" — requested after the two above:
  // "i search a specific place and send the mails based on it" / "i want in
  // the app only to make it search in more way now" — the direct fix for
  // "only searches one place at a time." Same auto-search shape again, but
  // the axis being swept is city, not niche/company name: one required
  // niche, an optional city list (blank -> CITIES, searchOptions.js).
  const [showMultiCitySearch, setShowMultiCitySearch] = useState(false);
  const [multiCityNiche, setMultiCityNiche] = useState('');
  const [multiCityCities, setMultiCityCities] = useState('');
  const [multiCitySearching, setMultiCitySearching] = useState(false);
  const [multiCityProgress, setMultiCityProgress] = useState(null);
  const [multiCityLastResult, setMultiCityLastResult] = useState('');
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
  // Which signal to sort the leads grid by, so bigger/more established
  // businesses can be worked first instead of whatever order the scraper
  // happened to return. 'reviews'/'rating' work on every lead the moment a
  // search returns (Google Maps already supplies both); 'capital'/'tier'
  // only have a real value once a lead has actually been audited (MCA
  // lookup and the rest of the budget signal both run during /api/audit),
  // so an un-audited lead sorts to the bottom on those two rather than
  // being mistaken for "confirmed small".
  const [leadSortKey, setLeadSortKey] = useState('default');

  const [historyLogs, setHistoryLogs] = useState([]);
  const [trackingEnabled, setTrackingEnabled] = useState(false);
  const [replyCheckEnabled, setReplyCheckEnabled] = useState(false);
  const [checkingReplies, setCheckingReplies] = useState(false);
  const [variantPerf, setVariantPerf] = useState([]);
  const [currentVariant, setCurrentVariant] = useState('');
  const [costLogs, setCostLogs] = useState([]);
  const [drafts, setDrafts] = useState([]);
  // Day/Week/Month/All section grouping for the Drafts and History tabs
  // (requested: "see day wise and arrange day week and month wise"),
  // independent per tab since a user reviewing today's drafts and skimming
  // a month of sent history are different tasks. Purely a rendering
  // concern — groupByPeriod() below buckets the same array the tab
  // already fetches, no backend change.
  const [draftsGroupBy, setDraftsGroupBy] = useState('day');
  // "Send All" bulk-send for the Drafts tab (requested: "in draft add send
  // all option"). Only ever touches the drafts currently visible under the
  // Day/Week/Month/All filter, and auto-acknowledges the review/staleness
  // gate on every one (the user's explicit choice — the per-draft
  // window.confirm in sendWithAcknowledgement doesn't scale to a batch).
  const [sendAllRunning, setSendAllRunning] = useState(false);
  const [sendAllProgress, setSendAllProgress] = useState({ done: 0, total: 0 });
  const sendAllStopRef = useRef(false);
  // Which batch is currently sending: '__all__' for the header "Send All",
  // or a day/week/month group's key for its own "Send day" button. Lets the
  // one running batch show progress + Stop while every other send button
  // disables (only one send loop at a time — they share the rate limit).
  const [sendAllGroupKey, setSendAllGroupKey] = useState(null);
  // Transient status line shown next to the progress count, e.g. while
  // waiting out the /api/send per-minute rate limiter before retrying.
  const [sendAllNote, setSendAllNote] = useState('');
  // Social Media page (2026-09-10) — its own search / results / drafts,
  // parallel to the website Dashboard. See renderSocial().
  const [socialNiche, setSocialNiche] = useState('');
  const [socialCity, setSocialCity] = useState('');
  const [socialLimit, setSocialLimit] = useState(10);
  const [socialBusinesses, setSocialBusinesses] = useState([]);
  const [socialSearching, setSocialSearching] = useState(false);
  const [socialSearchStage, setSocialSearchStage] = useState('');
  const [socialActiveChannel, setSocialActiveChannel] = useState({});
  const [socialDrafts, setSocialDrafts] = useState([]);
  const [socialDraftsGroupBy, setSocialDraftsGroupBy] = useState('day');
  const [historyGroupBy, setHistoryGroupBy] = useState('day');
  const [expandedEmail, setExpandedEmail] = useState(null);
  // Keyed by email_history row id -> { loading, sending, subject, body,
  // to_email, nextStage }. nextStage starts at 1 (first follow-up) and
  // becomes 2 once a follow-up has actually been sent for that row, so a
  // second click asks for the explicit-yes-or-no final follow-up instead
  // of re-offering help a second time.
  const [followupDrafts, setFollowupDrafts] = useState({});
  // Keyed by normaliseWebsiteKey(website) -> { id, company, subject, timestamp }
  // of the most recent email_history row for that site — lets a freshly
  // re-searched lead (a new object in `leads`, auditState: 'none') show it
  // was already emailed in a past session, which the session-local
  // lead.auditState === 'sent' badge alone can't see.
  const [sentWebsites, setSentWebsites] = useState({});
  // Same idea, one step earlier: every website with a draft already sitting
  // in email_drafts, so a re-searched lead shows "Draft already made"
  // instead of offering to generate one again from scratch.
  const [draftedWebsites, setDraftedWebsites] = useState({});
  // Set by the "View sent email" button; consumed by the scroll-into-view
  // effect once the History tab's rows are actually rendered.
  const [historyScrollTarget, setHistoryScrollTarget] = useState(null);
  // Same, for the "View draft" button and the Drafts tab.
  const [draftScrollTarget, setDraftScrollTarget] = useState(null);

  const isAutopilotRef = useRef(false);
  const leadsRef = useRef([]);
  // One cancel-control object per website with an audit in flight, so
  // handleCancelAudit (fired from a button, outside handleAudit's own
  // closure) can flag that specific poll loop to stop touching the lead —
  // see handleAudit's pollTimer and handleCancelAudit below.
  const auditControlRef = useRef({});

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
    if (currentView === 'social') {
      fetchSocialDrafts();
    }
    if (currentView === 'home' || currentView === 'agriculture') {
      fetchSentWebsites();
      fetchDraftedWebsites();
    }
  }, [currentView]);

  // Cross-references the leads grid against every website ever actually
  // sent to, so a lead re-scraped in a later session still shows "Already
  // sent" instead of a bare, misleading "Generate AI Audit & Draft" button.
  const fetchSentWebsites = () => {
    axios.get(`${API_BASE}/api/sent-websites?t=${Date.now()}`)
      .then(res => setSentWebsites(res.data || {}))
      .catch(console.error);
  };

  // Same, one step earlier: catches a lead re-scraped after a draft was
  // already made for that site in an earlier session.
  const fetchDraftedWebsites = () => {
    axios.get(`${API_BASE}/api/drafted-websites?t=${Date.now()}`)
      .then(res => setDraftedWebsites(res.data || {}))
      .catch(console.error);
  };

  // Jumps to the History tab and scrolls to the specific email_history row
  // — see the useEffect below that does the actual scrolling once History's
  // rows exist to scroll to.
  const goToSentEmail = (id) => {
    setExpandedEmail(id);
    setHistoryScrollTarget(id);
    setCurrentView('history');
  };

  // Same, for the Drafts tab.
  const goToDraft = (id) => {
    setDraftScrollTarget(id);
    setCurrentView('drafts');
  };

  useEffect(() => {
    if (currentView !== 'history' || !historyScrollTarget || historyLogs.length === 0) return;
    const el = document.getElementById(`history-${historyScrollTarget}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setHistoryScrollTarget(null);
  }, [currentView, historyLogs, historyScrollTarget]);

  useEffect(() => {
    if (currentView !== 'drafts' || !draftScrollTarget || drafts.length === 0) return;
    const el = document.getElementById(`draft-${draftScrollTarget}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setDraftScrollTarget(null);
  }, [currentView, drafts, draftScrollTarget]);

  // Also fetch once on mount, independent of currentView, so a lead already
  // in `leads` (restored from localStorage) shows its badge immediately.
  useEffect(() => {
    fetchSentWebsites();
    fetchDraftedWebsites();
  }, []);

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

  // Polls /api/search/progress + /api/search/result for an async_mode
  // search started with `key`, updating searchProgressNote as it goes, until
  // the result is ready — resolves with the leads array, or rejects with an
  // Error carrying the server's message. Shared by the plain Dashboard
  // search below and the MNC company-name lookup further down; both start
  // an async_mode search and need the identical wait-for-result loop. No
  // "seen it running yet" guard is needed the way /api/audit's poller needs
  // one — each key is a fresh token per request, never shared with an
  // earlier run, so there's no stale result it could race against.
  const pollSearchResult = (key) => new Promise((resolve, reject) => {
    const pollTimer = setInterval(async () => {
      try {
        const p = await axios.get(`${API_BASE}/api/search/progress`, { params: { key } });
        if (p.data?.running) setSearchProgressNote(p.data.stage || '');
        const r = await axios.get(`${API_BASE}/api/search/result`, { params: { key } });
        if (r.data?.ready) {
          clearInterval(pollTimer);
          if (r.data.error) reject(new Error(r.data.error));
          else resolve(r.data.leads || []);
        }
      } catch {
        // Progress/result polling is best-effort, matching handleAudit's
        // identical pattern — a failed poll just retries next tick.
      }
    }, 1500);
  });

  const handleSearch = async (e) => {
    e.preventDefault();
    setLoadingSearch(true);
    setSearchProgressNote('');
    setLeads([]);
    setLeadsPage(1);
    // A plain Home-tab search for an agri-sounding niche (e.g. typing
    // "agriculture" directly instead of using the dedicated Agriculture
    // tab) should still get the "Generate for Agriculture" button — tag
    // sector the same way the Agriculture tab's own searches do. Textile
    // (added 2026-09-08, no dedicated tab — just this tagging plus the
    // credibility line) works the same way off the same niche text box.
    const isAgriNiche = /agri|farm|krishi|agro/i.test(niche || '');
    const isTextileNiche = /textile|fabric|garment|apparel|yarn|weav|cotton/i.test(niche || '');
    try {
      const res = await axios.post(`${API_BASE}/api/search`, { niche, city, limit: parseInt(limit) || 10, async_mode: true });
      // Real work continues server-side when async_mode kicks in (see
      // SearchRequest.async_mode's docstring) — the Google Maps free-scraper
      // fallback shares a global Playwright semaphore with every
      // in-progress audit and can otherwise sit queued for minutes with
      // zero feedback, live-reported 2026-09-08.
      const rawLeads = res.data?.started ? await pollSearchResult(res.data.key) : res.data.leads;
      setLeads(rawLeads.map(lead => ({
        ...lead,
        auditState: 'none',
        ...(isAgriNiche ? { sector: 'agriculture', sectorDetail: niche } : {}),
        ...(isTextileNiche ? { sector: 'textile', sectorDetail: niche } : {}),
      })));
    } catch (err) {
      console.error('Search failed:', err);
      alert(`Error searching leads: ${err.response?.data?.detail || err.message}`);
    } finally {
      setLoadingSearch(false);
      setSearchProgressNote('');
    }
  };

  // One real /api/search lookup for one company name. Pulled out of
  // handleMncSearch so it's independently testable in shape and mirrors
  // runOneAgriSearch's role for the Agriculture tab's bulk runner.
  const runOneMncSearch = async (companyName, cityVal) => {
    const res = await axios.post(`${API_BASE}/api/search`, { niche: companyName, city: cityVal, limit: 1, async_mode: true });
    const rawLeads = res.data?.started ? await pollSearchResult(res.data.key) : res.data.leads;
    const tagged = rawLeads.map(lead => ({ ...lead, auditState: 'none', sourceType: 'mnc-lookup', sectorDetail: companyName }));
    setLeads(prev => [...tagged, ...prev]);
    return tagged.length;
  };

  // Runs one lookup per company name, sequentially, 13s apart — same
  // pacing/rationale as handleSearchAllNiches below: /api/search's rate
  // limit is 5 requests/60s, and this loop can otherwise burst well past it
  // for a real MNC list. One failed name doesn't stop the rest of the list.
  // Stoppable mid-run (mncStopRef), same ref-based pattern
  // agriBulkStopRef/isAutopilotRef already use to dodge a stale closure.
  //
  // Names box left blank -> MNC_NAMES (mncNames.js), a curated default list
  // of ~25 well-known MNCs, so "just give the location" (requested
  // 2026-09-08) actually works with only a city typed in. Typing a custom
  // list still overrides it, unchanged from before.
  const mncStopRef = useRef(false);
  const handleMncSearch = async (e) => {
    e.preventDefault();
    const typed = mncNames.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    const names = typed.length > 0 ? typed : MNC_NAMES;
    if (!mncCity.trim()) { alert('Enter a city first.'); return; }
    mncStopRef.current = false;
    setMncSearching(true);
    setMncLastResult('');
    let totalAdded = 0;
    try {
      for (let i = 0; i < names.length; i++) {
        if (mncStopRef.current) break;
        setMncProgress({ current: i + 1, total: names.length, label: names[i] });
        try {
          totalAdded += await runOneMncSearch(names[i], mncCity);
        } catch (err) {
          console.error(`MNC lookup failed for ${names[i]}:`, err);
        }
        setLeadsPage(1);
        if (i < names.length - 1 && !mncStopRef.current) await new Promise(r => setTimeout(r, 13000));
      }
    } finally {
      setMncSearching(false);
      setMncProgress(null);
      setMncLastResult(
        mncStopRef.current
          ? `Stopped early — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'} before stopping.`
          : `Looked up ${names.length} compan${names.length === 1 ? 'y' : 'ies'} — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'}.`
      );
    }
  };

  // One real /api/search lookup for one local-business niche, using the
  // main form's own Leads-per-search value (unlike runOneMncSearch's fixed
  // limit=1 — an ordinary niche search wants several candidates, not one
  // exact listing). Tags the same sector regexes handleSearch already uses
  // (a couple of NICHES entries, e.g. "Textile Manufacturer", genuinely
  // match them) so results stay consistent with a plain Dashboard search.
  const runOneLocalSearch = async (nicheChoice, cityVal) => {
    const res = await axios.post(`${API_BASE}/api/search`, { niche: nicheChoice, city: cityVal, limit: parseInt(limit) || 10, async_mode: true });
    const rawLeads = res.data?.started ? await pollSearchResult(res.data.key) : res.data.leads;
    const isAgriNiche = /agri|farm|krishi|agro/i.test(nicheChoice);
    const isTextileNiche = /textile|fabric|garment|apparel|yarn|weav|cotton/i.test(nicheChoice);
    const tagged = rawLeads.map(lead => ({
      ...lead,
      auditState: 'none',
      sourceType: 'local-search',
      ...(isAgriNiche ? { sector: 'agriculture', sectorDetail: nicheChoice } : {}),
      ...(isTextileNiche ? { sector: 'textile', sectorDetail: nicheChoice } : {}),
    }));
    setLeads(prev => [...tagged, ...prev]);
    return tagged.length;
  };

  // Runs one search per niche, sequentially, 13s apart — identical
  // pacing/stop pattern to handleMncSearch/handleSearchAllNiches.
  // Niches box left blank -> the full NICHES list (searchOptions.js);
  // typing a custom list overrides it.
  const localStopRef = useRef(false);
  const handleLocalSearch = async (e) => {
    e.preventDefault();
    const typed = localNiches.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    const nichesToRun = typed.length > 0 ? typed : NICHES;
    if (!localCity.trim()) { alert('Enter a city first.'); return; }
    localStopRef.current = false;
    setLocalSearching(true);
    setLocalLastResult('');
    let totalAdded = 0;
    try {
      for (let i = 0; i < nichesToRun.length; i++) {
        if (localStopRef.current) break;
        setLocalProgress({ current: i + 1, total: nichesToRun.length, label: nichesToRun[i] });
        try {
          totalAdded += await runOneLocalSearch(nichesToRun[i], localCity);
        } catch (err) {
          console.error(`Local search failed for ${nichesToRun[i]}:`, err);
        }
        setLeadsPage(1);
        if (i < nichesToRun.length - 1 && !localStopRef.current) await new Promise(r => setTimeout(r, 13000));
      }
    } finally {
      setLocalSearching(false);
      setLocalProgress(null);
      setLocalLastResult(
        localStopRef.current
          ? `Stopped early — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'} before stopping.`
          : `Searched ${nichesToRun.length} niche${nichesToRun.length === 1 ? '' : 's'} — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'}.`
      );
    }
  };

  // One real /api/search lookup for one niche in one city — the same shape
  // as runOneLocalSearch, just keyed by city in the loop instead of niche.
  const runOneMultiCitySearch = async (nicheVal, cityVal) => {
    const res = await axios.post(`${API_BASE}/api/search`, { niche: nicheVal, city: cityVal, limit: parseInt(limit) || 10, async_mode: true });
    const rawLeads = res.data?.started ? await pollSearchResult(res.data.key) : res.data.leads;
    const isAgriNiche = /agri|farm|krishi|agro/i.test(nicheVal);
    const isTextileNiche = /textile|fabric|garment|apparel|yarn|weav|cotton/i.test(nicheVal);
    const tagged = rawLeads.map(lead => ({
      ...lead,
      auditState: 'none',
      sourceType: 'multi-city-search',
      ...(isAgriNiche ? { sector: 'agriculture', sectorDetail: nicheVal } : {}),
      ...(isTextileNiche ? { sector: 'textile', sectorDetail: nicheVal } : {}),
    }));
    setLeads(prev => [...tagged, ...prev]);
    return tagged.length;
  };

  // Same pacing/stop pattern as handleLocalSearch, swept over cities
  // instead of niches. Cities box left blank -> the full CITIES list
  // (searchOptions.js, Maharashtra-wide); typing a custom list overrides it.
  const multiCityStopRef = useRef(false);
  const handleMultiCitySearch = async (e) => {
    e.preventDefault();
    if (!multiCityNiche.trim()) { alert('Enter a niche first.'); return; }
    const typed = multiCityCities.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    const citiesToRun = typed.length > 0 ? typed : CITIES;
    multiCityStopRef.current = false;
    setMultiCitySearching(true);
    setMultiCityLastResult('');
    let totalAdded = 0;
    try {
      for (let i = 0; i < citiesToRun.length; i++) {
        if (multiCityStopRef.current) break;
        setMultiCityProgress({ current: i + 1, total: citiesToRun.length, label: citiesToRun[i] });
        try {
          totalAdded += await runOneMultiCitySearch(multiCityNiche, citiesToRun[i]);
        } catch (err) {
          console.error(`Multi-city search failed for ${citiesToRun[i]}:`, err);
        }
        setLeadsPage(1);
        if (i < citiesToRun.length - 1 && !multiCityStopRef.current) await new Promise(r => setTimeout(r, 13000));
      }
    } finally {
      setMultiCitySearching(false);
      setMultiCityProgress(null);
      setMultiCityLastResult(
        multiCityStopRef.current
          ? `Stopped early — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'} before stopping.`
          : `Searched "${multiCityNiche}" across ${citiesToRun.length} cit${citiesToRun.length === 1 ? 'y' : 'ies'} — added ${totalAdded} lead${totalAdded === 1 ? '' : 's'}.`
      );
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
  // includeAgriCredibility/includeTextileCredibility are null on a plain
  // call (e.g. Retry Audit) — that means "keep whatever this lead was
  // already set to", so retrying a failed credibility-line audit doesn't
  // silently drop the line. Only the dedicated Generate buttons below ever
  // pass an explicit true/false.
  const handleAudit = async (index, force = false, includeAgriCredibility = null, includeTextileCredibility = null) => {
    const lead = leadsRef.current[index];
    const effectiveIncludeAgri = includeAgriCredibility !== null
      ? includeAgriCredibility
      : !!lead.includeAgriCredibility;
    const effectiveIncludeTextile = includeTextileCredibility !== null
      ? includeTextileCredibility
      : !!lead.includeTextileCredibility;
    // Each lead can carry up to three independently-generated drafts (plain /
    // agriculture-credibility / textile-credibility — the two credibility
    // flags are mutually exclusive per sector, so this covers every real
    // combination). Reported live: switching from Agriculture to Textile and
    // back on the same lead re-ran the whole paid audit+AI pipeline for a
    // draft that had already been generated. `auditVariants` caches the
    // finished draft per variant key on the lead object itself, so it rides
    // along with the existing leads->localStorage persistence for free (a
    // page refresh doesn't lose the cache either). Retry Audit (force=true)
    // is the only way to bypass it, and only overwrites that one variant's
    // cache entry.
    const variantKey = effectiveIncludeAgri ? 'agri' : effectiveIncludeTextile ? 'textile' : 'plain';
    const cachedVariant = lead.auditVariants?.[variantKey];
    if (!force && cachedVariant) {
      setLeads(prev => {
        const newLeads = [...prev];
        newLeads[index].auditState = 'done';
        newLeads[index].auditProgress = null;
        newLeads[index].auditData = cachedVariant;
        newLeads[index].includeAgriCredibility = effectiveIncludeAgri;
        newLeads[index].includeTextileCredibility = effectiveIncludeTextile;
        return newLeads;
      });
      return;
    }
    setLeads(prev => {
      const newLeads = [...prev];
      newLeads[index].auditState = 'auditing';
      newLeads[index].auditProgress = null;
      newLeads[index].includeAgriCredibility = effectiveIncludeAgri;
      newLeads[index].includeTextileCredibility = effectiveIncludeTextile;
      return newLeads;
    });

    // An audit takes a couple of minutes (the tool timeouts were raised for
    // accuracy), and Railway's edge times out the underlying HTTP request
    // well before that — live-confirmed via the deploy log (the audit was
    // still cleanly progressing at 65s+ in, no backend error, no restart;
    // the connection was cut in front of the app). So for any lead with a
    // website, /api/audit is called with async_mode: true — it returns
    // {"started": true} almost immediately instead of blocking, and the
    // real result is picked up here via polling once /api/audit/progress
    // reports the run has finished. A lead with no website skips this
    // entirely (the backend gates async_mode on req.website too, and that
    // path is fast anyway with no Playwright/Lighthouse involved).
    let pollTimer = null;
    let resolved = false;
    let sawRunning = false;
    // Once we've seen the run live, a stretch of polls where progress is
    // gone AND no result ever lands means the run died without running its
    // own cleanup — almost always the server restarting mid-audit (a
    // deploy). Without a bail-out here the card freezes on its last
    // "Step N of 6" forever, and Cancel can't rescue it either: the task
    // /api/audit/cancel looks for went with the old process. Count those
    // polls; act after ~12s of grace (a brief between-stages gap looks the
    // same for a tick or two).
    let stalledTicks = 0;
    const STALLED_TICK_LIMIT = 8;
    // See auditControlRef's declaration — lets handleCancelAudit reach into
    // this specific poll loop from outside its closure.
    const control = { cancelled: false };
    if (lead.Website) auditControlRef.current[lead.Website] = control;

    const finishAudit = (payload) => {
      resolved = true;
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (lead.Website) delete auditControlRef.current[lead.Website];
      setLeads(prev => {
        const updatedLeads = [...prev];
        if (payload.cancelled) {
          // Back to the pre-audit state, not 'failed' — cancelling isn't an
          // error, and a stuck "Retry Audit" button would be a worse UX
          // than just letting "Generate AI Audit & Draft" reappear.
          updatedLeads[index].auditState = 'none';
        } else if (payload.error) {
          updatedLeads[index].auditState = 'failed';
          updatedLeads[index].auditError = payload.error;
        } else {
          updatedLeads[index].auditState = 'done';
          updatedLeads[index].auditData = payload;
          // Cache this real draft under its variant key so re-clicking the
          // same button (or navigating away and back) doesn't re-run the
          // pipeline — see the cache-check above.
          updatedLeads[index].auditVariants = {
            ...updatedLeads[index].auditVariants,
            [variantKey]: payload,
          };
        }
        updatedLeads[index].auditProgress = null;
        return updatedLeads;
      });
      if (!payload.error && !payload.cancelled) fetchDraftedWebsites(); // refresh "Draft already made" for this website right away
    };

    if (lead.Website) {
      pollTimer = setInterval(async () => {
        if (resolved) return;
        if (control.cancelled) {
          // handleCancelAudit already reset the lead's UI state directly —
          // this loop has nothing left to do but stop polling.
          clearInterval(pollTimer);
          return;
        }
        try {
          const p = await axios.get(`${API_BASE}/api/audit/progress`, {
            params: { website: lead.Website }
          });
          if (p.data?.running) {
            sawRunning = true;
            stalledTicks = 0;
            setLeads(prev => {
              const updated = [...prev];
              if (updated[index]?.auditState === 'auditing') {
                updated[index].auditProgress = p.data;
              }
              return updated;
            });
            return;
          }
          // Not running: either the background task hasn't started yet
          // (async_mode POST raced ahead of the first progress write) or it
          // just finished. Only check for a result once we've actually seen
          // it running, so an early poll doesn't mistake "not started" for
          // "done with nothing".
          if (!sawRunning) return;
          const r = await axios.get(`${API_BASE}/api/audit/result`, {
            params: { website: lead.Website }
          });
          if (r.data?.ready) {
            stalledTicks = 0;
            finishAudit(r.data);
            return;
          }
          // Seen running, progress now gone, still no result after the
          // grace window — the run is dead (server restarted mid-audit).
          // Recover a draft if the backend managed to save one before it
          // went, otherwise surface a real failure so the card unfreezes
          // and Retry Audit works again.
          stalledTicks += 1;
          if (stalledTicks >= STALLED_TICK_LIMIT) {
            let recovered = null;
            try {
              const rec = await axios.get(`${API_BASE}/api/audit/recover`, {
                params: { website: lead.Website }
              });
              recovered = rec.data?.draft || null;
            } catch {
              // Can't tell — fall through to the failed state below.
            }
            if (recovered) {
              finishAudit({
                email: recovered.target_email,
                subject: recovered.subject,
                body: recovered.body,
                image_url: recovered.image_url || null,
                review_warnings: recovered.review_warnings || [],
                page_speed_score: null,
                seo_score: null,
                recoveredAfterDroppedConnection: true,
              });
            } else {
              finishAudit({ error: 'The audit stopped unexpectedly — the server may have restarted mid-run. Click Retry Audit.' });
            }
          }
        } catch {
          // Progress/result polling is best-effort — never let a failed
          // poll disturb the in-flight audit; the next tick tries again.
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
        sector_detail: lead.sectorDetail || '',
        include_agri_credibility: effectiveIncludeAgri,
        include_textile_credibility: effectiveIncludeTextile,
        async_mode: !!lead.Website,
      });

      if (res.data?.started) {
        // Real work continues server-side; the poll timer above picks up
        // the result via /api/audit/result once it's ready. Nothing more
        // to do on this call.
        return;
      }
      finishAudit(res.data);
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

      if (recovered) {
        finishAudit({
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
        });
      } else {
        finishAudit({ error: err.response?.data?.detail || err.message });
      }
    }
  };

  // Requested as "add an option of cancelling the ongoing audit". Confirms
  // the backend actually cancelled something BEFORE resetting the lead's
  // UI state — reported live as "even after cancelling it's still
  // running": this used to reset the card to 'none' unconditionally, the
  // instant the button was clicked, regardless of what /api/audit/cancel
  // actually reported back. If the cancel came back {"cancelled": false}
  // (the task had already finished, or — the other half of this same
  // fix, see app.py — a second /api/audit call for this website had
  // orphaned the one this button meant to stop) or the request itself
  // failed, the card still went straight back to "Generate AI Audit &
  // Draft" as if nothing was happening, while the real audit kept running
  // server-side and could still land a draft moments later with no
  // warning it had. Now the poll loop (see the `control` object
  // handleAudit registers) is only stopped once the server confirms it
  // actually cancelled something.
  const handleCancelAudit = async (index) => {
    const lead = leadsRef.current[index];
    if (!lead.Website) return;

    const stopThisCard = () => {
      const control = auditControlRef.current[lead.Website];
      if (control) control.cancelled = true;
      setLeads(prev => {
        const updated = [...prev];
        if (updated[index]?.auditState === 'auditing') {
          updated[index].auditState = 'none';
          updated[index].auditProgress = null;
        }
        return updated;
      });
    };

    // When cancel can't confirm a stop (server had no live task — usually
    // it restarted mid-audit and the card is now frozen on a run that no
    // longer exists), don't leave a dead button: check whether anything is
    // actually still running, and if not, clear the frozen card. Only a
    // genuine {"running": true} keeps the card as-is.
    const clearIfNothingRunning = async () => {
      try {
        const p = await axios.get(`${API_BASE}/api/audit/progress`, { params: { website: lead.Website } });
        if (!p.data?.running) stopThisCard();
      } catch {
        // Can't reach progress either — a stuck card with a dead button is
        // worse than an over-eager reset.
        stopThisCard();
      }
    };

    try {
      const res = await axios.post(`${API_BASE}/api/audit/cancel`, null, { params: { website: lead.Website } });
      if (res.data?.cancelled !== true) {
        await clearIfNothingRunning();
        return;
      }
    } catch (err) {
      console.error('Cancel audit failed:', err);
      await clearIfNothingRunning();
      return;
    }
    // Server confirmed it stopped a live task.
    stopThisCard();
  };

  // handleAudit's promise resolves the instant its async_mode POST replies
  // {"started": true} (added 2026-09-07) — long before the real audit
  // finishes. Reported live: Autopilot was firing every queued lead's audit
  // within a couple seconds of each other, and since they all share one
  // global _PLAYWRIGHT_SEMAPHORE(1) for their very first step (screenshot
  // capture), most of them just piled up behind each other looking
  // permanently frozen at "Step 1 of 6" instead of visibly one-at-a-time.
  // This restores the one-at-a-time behaviour Autopilot always intended by
  // waiting for the lead's own auditState to actually leave 'auditing' —
  // same sawRunning-style "must have seen it start before treating a
  // not-auditing read as done" guard handleAudit's own poll loop uses,
  // needed here too since setLeads('auditing') and leadsRef's sync effect
  // are both async relative to this loop.
  const waitForAuditToFinish = (index, timeoutMs = 5 * 60 * 1000) => new Promise((resolve) => {
    const start = Date.now();
    let sawAuditing = false;
    const check = () => {
      const state = leadsRef.current[index]?.auditState;
      if (state === 'auditing') sawAuditing = true;
      if (
        (sawAuditing && state !== 'auditing') ||
        Date.now() - start > timeoutMs ||
        !isAutopilotRef.current // "Stop Autopilot" shouldn't wait out this lead first
      ) {
        resolve();
        return;
      }
      setTimeout(check, 500);
    };
    check();
  });

  const startAutopilot = async () => {
    setIsAutopilot(true);
    for (let i = 0; i < leadsRef.current.length; i++) {
      if (!isAutopilotRef.current) break;
      const lead = leadsRef.current[i];
      if (lead.auditState === 'none' && lead.Website) {
        await handleAudit(i);
        await waitForAuditToFinish(i);
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

  // Buckets a list of drafts/history rows into Day/Week/Month sections for
  // the Drafts and History tabs' group-by control. `getTimestamp` pulls the
  // sortable date string off each item (both tabs' rows already carry one —
  // `timestamp`, "YYYY-MM-DD HH:MM:SS"). Parsed the same way draftAgeDays()
  // above already does (no explicit UTC 'Z', so this reads it as local time
  // in the browser) — deliberately consistent with the existing age badge
  // rather than "more correct but disagrees with it". Rows the caller's own
  // ordering already sorted newest-first stay in that order within each
  // bucket; only bucket order (newest section first) is computed here.
  // period === 'all' is a no-op passthrough, one section holding everything,
  // so callers can render through the same grouped shape either way.
  const groupByPeriod = (items, period, getTimestamp = (item) => item.timestamp) => {
    if (period === 'all') {
      return items.length ? [{ key: 'all', label: null, items }] : [];
    }
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const buckets = new Map(); // insertion order == first-seen order == newest-first, since items arrive newest-first
    for (const item of items) {
      const raw = getTimestamp(item);
      const parsed = raw ? new Date(String(raw).replace(' ', 'T')) : null;
      const valid = parsed && !isNaN(parsed);
      let key, label;
      if (!valid) {
        key = '_unknown';
        label = 'Unknown date';
      } else if (period === 'day') {
        const day = new Date(parsed); day.setHours(0, 0, 0, 0);
        key = day.toISOString().slice(0, 10);
        const diffDays = Math.round((today - day) / 86400000);
        label = diffDays === 0 ? 'Today' : diffDays === 1 ? 'Yesterday'
          : day.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric', year: day.getFullYear() !== today.getFullYear() ? 'numeric' : undefined });
      } else if (period === 'week') {
        // Monday-start week, matching ISO/most calendars this tool's users are in.
        const day = new Date(parsed); day.setHours(0, 0, 0, 0);
        const dow = (day.getDay() + 6) % 7; // 0 = Monday
        const monday = new Date(day); monday.setDate(day.getDate() - dow);
        const sunday = new Date(monday); sunday.setDate(monday.getDate() + 6);
        key = monday.toISOString().slice(0, 10);
        const fmt = (d) => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        label = `Week of ${fmt(monday)} – ${fmt(sunday)}${monday.getFullYear() !== today.getFullYear() ? `, ${monday.getFullYear()}` : ''}`;
      } else { // 'month'
        key = `${parsed.getFullYear()}-${String(parsed.getMonth() + 1).padStart(2, '0')}`;
        label = parsed.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
      }
      if (!buckets.has(key)) buckets.set(key, { key, label, items: [] });
      buckets.get(key).items.push(item);
    }
    return Array.from(buckets.values());
  };

  // Small pill row shared by the Drafts and History tabs' group-by control.
  const GroupBySelector = ({ value, onChange }) => (
    <div style={{ display: 'flex', gap: '6px' }}>
      {['day', 'week', 'month', 'all'].map(opt => (
        <button
          key={opt}
          onClick={() => onChange(opt)}
          style={{
            padding: '6px 14px', borderRadius: '999px', fontSize: '13px', cursor: 'pointer',
            border: value === opt ? '1px solid rgba(16,185,129,0.5)' : '1px solid rgba(148,163,184,0.25)',
            background: value === opt ? 'rgba(16,185,129,0.15)' : 'rgba(148,163,184,0.08)',
            color: value === opt ? '#10b981' : '#94a3b8',
            fontWeight: value === opt ? 600 : 400,
            textTransform: 'capitalize',
          }}
        >{opt === 'all' ? 'All' : opt}</button>
      ))}
    </div>
  );

  // /api/send returns 409 when the draft carries unacknowledged review
  // warnings or is old enough that its audit data may be stale. That's a
  // deliberate stop, not an error: show the human exactly what was flagged
  // and only retry if they explicitly choose to send anyway.
  const sendWithAcknowledgement = async (payload) => {
    try {
      const res = await axios.post(`${API_BASE}/api/send`, payload);
      fetchSentWebsites(); // refresh the "Already sent" badge for this website right away
      return res;
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

      const res = await axios.post(`${API_BASE}/api/send`, { ...payload, acknowledge_warnings: true });
      fetchSentWebsites();
      return res;
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
      fetchDraftedWebsites();
    } catch (err) {
      console.error('Draft send failed:', err);
      alert(`Failed to send draft email: ${err.response?.data?.detail || err.message}`);
      setDrafts(originalDrafts);
    }
  };

  // Bulk-send every draft in the current Day/Week/Month/All view, paced
  // ~7s apart (/api/send is rate-limited to 10/min, and there's a
  // server-side DAILY_EMAIL_LIMIT cap on top). Auto-acknowledges the
  // review/staleness 409 gate on every draft — the user chose "send
  // everything" over "skip flagged". Stoppable mid-run via sendAllStopRef
  // (same ref pattern as the Dashboard bulk runners). One failed send
  // doesn't abort the rest; a 429 stops the whole run (rate limit or the
  // daily cap — retrying just burns more of it).
  const handleSendAll = async (draftsInView, groupKey = '__all__', groupLabel = '') => {
    const sendable = draftsInView.filter(d => d.target_email);
    if (sendable.length === 0) {
      alert('No drafts with a recipient address in this batch.');
      return;
    }
    const scope = groupLabel ? `all ${sendable.length} draft${sendable.length === 1 ? '' : 's'} under "${groupLabel}"`
                             : `all ${sendable.length} draft${sendable.length === 1 ? '' : 's'} in this view`;
    if (!window.confirm(
      `Send ${scope} now?\n\n` +
      `Drafts you removed the image from are sent without the attachment. ` +
      `Any flagged "Review before sending" or stale drafts are included and sent anyway.`
    )) return;

    sendAllStopRef.current = false;
    setSendAllRunning(true);
    setSendAllGroupKey(groupKey);
    setSendAllNote('');
    setSendAllProgress({ done: 0, total: sendable.length });

    let sent = 0;
    let failed = 0;
    let stoppedAtDailyCap = false;
    let rateLimitWaits = 0;

    for (let idx = 0; idx < sendable.length; idx++) {
      if (sendAllStopRef.current) break;
      const draft = sendable[idx];
      try {
        await axios.post(`${API_BASE}/api/send`, {
          email: draft.target_email,
          subject: draft.subject,
          body: draft.body,
          company: draft.company,
          website: draft.website,
          attach_screenshot: draft.attach_screenshot !== false,
          acknowledge_warnings: true,
        });
        sent++;
        rateLimitWaits = 0;
        setDrafts(prev => prev.filter(d => d.id !== draft.id));
      } catch (err) {
        const detail = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : '';
        if (err.response?.status === 429 && /daily sending limit/i.test(detail)) {
          // The real cap — SES warm-up ceiling. Retrying just 429s again
          // until the rolling 24h window clears, so stop the whole run.
          stoppedAtDailyCap = true;
          break;
        }
        if (err.response?.status === 429) {
          // The /api/send per-minute rate limiter (10/min), not the daily
          // cap. Transient — usually the bucket was already partly full
          // from earlier clicks, which used to kill the batch on send #1.
          // Wait it out and retry THIS same draft instead of losing the
          // rest of the batch.
          rateLimitWaits++;
          if (rateLimitWaits > 6 || sendAllStopRef.current) { stoppedAtDailyCap = false; break; }
          setSendAllNote('rate limited — waiting 60s, will continue automatically');
          await new Promise(r => setTimeout(r, 62000));
          setSendAllNote('');
          idx--;            // retry the same draft on the next iteration
          continue;         // skip the normal 7s pacing wait below
        }
        console.error(`Send All: failed for ${draft.company}:`, err);
        failed++;
      }
      setSendAllProgress({ done: idx + 1, total: sendable.length });
      // Pace the next one — 10/min limit is 6s/send, 7s leaves margin.
      if (idx < sendable.length - 1 && !sendAllStopRef.current) {
        await new Promise(r => setTimeout(r, 7000));
      }
    }

    setSendAllRunning(false);
    setSendAllGroupKey(null);
    setSendAllNote('');
    fetchSentWebsites();
    fetchDraftedWebsites();
    // Let React paint the cleared "Sending…" state before the blocking alert.
    await new Promise(r => setTimeout(r, 0));
    alert(
      `Send All finished.\n\nSent: ${sent}` +
      (failed ? `\nFailed: ${failed}` : '') +
      (stoppedAtDailyCap ? `\nStopped: hit the daily sending cap (SES warm-up limit) — run again tomorrow for the rest.` : '') +
      (rateLimitWaits > 6 ? `\nStopped: the send endpoint kept rate-limiting — wait a few minutes and run again for the rest.` : '') +
      (sendAllStopRef.current ? `\nStopped early by you.` : '')
    );
  };

  const handleDraftDelete = async (draftId) => {
    try {
      await axios.delete(`${API_BASE}/api/drafts/${draftId}`);
      setDrafts(drafts.filter(d => d.id !== draftId));
      fetchDraftedWebsites();
    } catch (err) {
      console.error('Draft delete failed:', err);
      alert(`Failed to delete draft: ${err.response?.data?.detail || err.message}`);
    }
  };

  // Indices into `leads`, not the leads themselves — every click handler
  // below (handleAudit(i, ...), newLeads[i]..., etc.) already addresses a
  // lead by its position in the real `leads` array, and that can't change
  // just because the grid is displaying it in a different order. Sorting a
  // copy of the leads and losing track of each one's real index would mutate
  // the wrong lead the moment two cards swap position on screen.
  const sortedLeadIndices = useMemo(() => {
    const indices = leads.map((_, i) => i);
    const scoreFn = LEAD_SORT_COMPARATORS[leadSortKey];
    if (!scoreFn) return indices; // 'default' — as returned by the search
    // Stable-ish: ties keep their original relative order since Array#sort
    // in modern engines is a stable sort and neither score nor original
    // index is ever equal-but-reordered here.
    return indices.sort((a, b) => scoreFn(leads[b]) - scoreFn(leads[a]));
  }, [leads, leadSortKey]);

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
          <label>City (or a whole state)</label>
          <input
            type="text" list="city-options" value={city} onChange={e => setCity(e.target.value)}
            placeholder="e.g. Mumbai, or Maharashtra for the whole state" required
            title="Type a city for a local search, or a state name (e.g. Maharashtra) to search the whole state in one go — useful for finding bigger, more established businesses beyond any single city."
          />
          <datalist id="city-options">
            {CITIES.map(c => <option key={c} value={c} />)}
          </datalist>
        </div>
        <div className="input-group" style={{maxWidth: '100px'}}>
          <label>Leads</label>
          <input type="number" value={limit} onChange={e => setLimit(e.target.value)} min="1" max="100" required />
        </div>
        {/* flexWrap is load-bearing: .search-box is itself an unwrapped flex
            row inside a 1200px-max .app-container, and body has
            overflow-x:hidden — with five buttons now in this group (Find
            Leads, +Specific Lead, Search MNCs, Search Local Businesses, One
            Niche Many Cities), an unwrapped row silently clipped the last
            two off-screen with no scrollbar to reveal them. Live-reported
            2026-09-09: the deploy was confirmed live and correct in Railway,
            but two of five buttons were simply invisible. */}
        <div style={{ display: 'flex', gap: '12px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <button type="submit" className="primary-btn" disabled={loadingSearch}>
            {loadingSearch ? <Loader2 className="spin" /> : <Search />}
            {loadingSearch ? 'Scraping...' : 'Find Leads'}
          </button>
          <button type="button" onClick={() => setShowManualEntry(!showManualEntry)} style={{ background: showManualEntry ? '#fee2e2' : '#f8fafc', border: showManualEntry ? '1px solid #f87171' : '1px solid #cbd5e1', color: showManualEntry ? '#ef4444' : '#334155', padding: '0 20px', borderRadius: '12px', cursor: 'pointer', height: '48px', fontSize: '15px', fontWeight: 'bold', transition: 'all 0.2s', whiteSpace: 'nowrap', boxShadow: '0 2px 4px rgba(0,0,0,0.05)' }}>
            {showManualEntry ? 'Cancel' : '+ Specific Lead'}
          </button>
          {/* Opening a panel seeds its City/Niche field from whatever's
              already typed in the main form above — live-reported
              2026-09-09: typing a niche up top, then opening one of these
              panels, looked like "nothing happened" because each panel has
              its own separate fields that started empty regardless of what
              was already typed. Only seeds an EMPTY field, never overwrites
              something the panel itself already holds. */}
          <button type="button" onClick={() => { if (!showMncSearch && !mncCity) setMncCity(city); setShowMncSearch(!showMncSearch); }} style={{ background: showMncSearch ? '#fee2e2' : '#f8fafc', border: showMncSearch ? '1px solid #f87171' : '1px solid #cbd5e1', color: showMncSearch ? '#ef4444' : '#334155', padding: '0 20px', borderRadius: '12px', cursor: 'pointer', height: '48px', fontSize: '15px', fontWeight: 'bold', transition: 'all 0.2s', whiteSpace: 'nowrap', boxShadow: '0 2px 4px rgba(0,0,0,0.05)' }}>
            {showMncSearch ? 'Cancel' : 'Search MNCs'}
          </button>
          <button type="button" onClick={() => { if (!showLocalSearch && !localCity) setLocalCity(city); setShowLocalSearch(!showLocalSearch); }} style={{ background: showLocalSearch ? '#fee2e2' : '#f8fafc', border: showLocalSearch ? '1px solid #f87171' : '1px solid #cbd5e1', color: showLocalSearch ? '#ef4444' : '#334155', padding: '0 20px', borderRadius: '12px', cursor: 'pointer', height: '48px', fontSize: '15px', fontWeight: 'bold', transition: 'all 0.2s', whiteSpace: 'nowrap', boxShadow: '0 2px 4px rgba(0,0,0,0.05)' }}>
            {showLocalSearch ? 'Cancel' : 'Search Local Businesses'}
          </button>
          <button type="button" onClick={() => { if (!showMultiCitySearch && !multiCityNiche) setMultiCityNiche(niche); setShowMultiCitySearch(!showMultiCitySearch); }} style={{ background: showMultiCitySearch ? '#fee2e2' : '#f8fafc', border: showMultiCitySearch ? '1px solid #f87171' : '1px solid #cbd5e1', color: showMultiCitySearch ? '#ef4444' : '#334155', padding: '0 20px', borderRadius: '12px', cursor: 'pointer', height: '48px', fontSize: '15px', fontWeight: 'bold', transition: 'all 0.2s', whiteSpace: 'nowrap', boxShadow: '0 2px 4px rgba(0,0,0,0.05)' }}>
            {showMultiCitySearch ? 'Cancel' : 'One Niche, Many Cities'}
          </button>
        </div>
      </form>
      {loadingSearch && searchProgressNote && (
        <p style={{ margin: '8px 0 0', fontSize: '13px', color: '#94a3b8' }}>{searchProgressNote}</p>
      )}

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

      <AnimatePresence>
        {showMncSearch && (
          <motion.form initial={{ opacity: 0, height: 0, marginTop: 0 }} animate={{ opacity: 1, height: 'auto', marginTop: 16 }} exit={{ opacity: 0, height: 0, marginTop: 0 }} className="search-box glass" style={{ overflow: 'hidden', flexWrap: 'wrap' }} onSubmit={handleMncSearch}>
            <div style={{ width: '100%', fontSize: 13, color: '#64748b', marginBottom: 4 }}>
              Just give a city — this searches a built-in list of ~{MNC_NAMES.length} well-known MNCs there automatically.
              Only type your own names below if you want a specific custom list instead.
            </div>
            <div className="input-group" style={{ flex: 2, minWidth: 240 }}>
              <label>Company Names <span style={{ fontWeight: 400, color: '#94a3b8' }}>(optional — leave blank for the default MNC list)</span></label>
              <textarea
                rows={2}
                value={mncNames}
                onChange={e => setMncNames(e.target.value)}
                placeholder={"Leave blank to search the default list, or type your own:\nGoogle India\nMicrosoft India"}
                style={{ width: '100%', padding: '10px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', fontFamily: 'inherit', fontSize: '14px', resize: 'vertical' }}
              />
            </div>
            <div className="input-group" style={{ maxWidth: 200 }}>
              <label>City (or a whole state)</label>
              <input type="text" list="city-options" value={mncCity} onChange={e => setMncCity(e.target.value)} placeholder="e.g. Bengaluru" />
            </div>
            <button type="submit" className="primary-btn" disabled={mncSearching} style={{ background: mncSearching ? '#94a3b8' : '#4f46e5' }}>
              {mncSearching ? <Loader2 className="spin" /> : <Search />}
              {mncSearching ? 'Searching...' : 'Search MNCs'}
            </button>
            {mncSearching && (
              <button type="button" onClick={() => { mncStopRef.current = true; }} style={{ padding: '0 16px', background: '#fee2e2', border: '1px solid #f87171', color: '#ef4444', borderRadius: '12px', cursor: 'pointer', fontWeight: 'bold' }}>
                Stop
              </button>
            )}
            {mncProgress && (
              <p style={{ width: '100%', margin: '4px 0 0', fontSize: 13, color: '#94a3b8' }}>
                {mncProgress.current}/{mncProgress.total}: {mncProgress.label}{searchProgressNote ? ` — ${searchProgressNote}` : ''}
              </p>
            )}
            {!mncSearching && mncLastResult && (
              <p style={{ width: '100%', margin: '4px 0 0', fontSize: 13, color: '#10b981' }}>{mncLastResult}</p>
            )}
          </motion.form>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {showLocalSearch && (
          <motion.form initial={{ opacity: 0, height: 0, marginTop: 0 }} animate={{ opacity: 1, height: 'auto', marginTop: 16 }} exit={{ opacity: 0, height: 0, marginTop: 0 }} className="search-box glass" style={{ overflow: 'hidden', flexWrap: 'wrap' }} onSubmit={handleLocalSearch}>
            <div style={{ width: '100%', fontSize: 13, color: '#64748b', marginBottom: 4 }}>
              Just give a city — this runs every niche in the built-in list ({NICHES.length} categories: health, fitness, food,
              retail, trades, professional services and more) there automatically. A full run takes 30+ minutes (stoppable
              anytime) since it's paced to respect the search rate limit. Type your own niches below to run a shorter custom list instead.
            </div>
            <div className="input-group" style={{ flex: 2, minWidth: 240 }}>
              <label>Niches <span style={{ fontWeight: 400, color: '#94a3b8' }}>(optional — leave blank for the full built-in list)</span></label>
              <textarea
                rows={2}
                value={localNiches}
                onChange={e => setLocalNiches(e.target.value)}
                placeholder={"Leave blank to search every built-in niche, or type your own:\nDentist\nSalon\nGym"}
                style={{ width: '100%', padding: '10px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', fontFamily: 'inherit', fontSize: '14px', resize: 'vertical' }}
              />
            </div>
            <div className="input-group" style={{ maxWidth: 200 }}>
              <label>City (or a whole state)</label>
              <input type="text" list="city-options" value={localCity} onChange={e => setLocalCity(e.target.value)} placeholder="e.g. Pune" />
            </div>
            <button type="submit" className="primary-btn" disabled={localSearching} style={{ background: localSearching ? '#94a3b8' : '#0891b2' }}>
              {localSearching ? <Loader2 className="spin" /> : <Search />}
              {localSearching ? 'Searching...' : 'Search Local Businesses'}
            </button>
            {localSearching && (
              <button type="button" onClick={() => { localStopRef.current = true; }} style={{ padding: '0 16px', background: '#fee2e2', border: '1px solid #f87171', color: '#ef4444', borderRadius: '12px', cursor: 'pointer', fontWeight: 'bold' }}>
                Stop
              </button>
            )}
            {localProgress && (
              <p style={{ width: '100%', margin: '4px 0 0', fontSize: 13, color: '#94a3b8' }}>
                {localProgress.current}/{localProgress.total}: {localProgress.label}{searchProgressNote ? ` — ${searchProgressNote}` : ''}
              </p>
            )}
            {!localSearching && localLastResult && (
              <p style={{ width: '100%', margin: '4px 0 0', fontSize: 13, color: '#10b981' }}>{localLastResult}</p>
            )}
          </motion.form>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {showMultiCitySearch && (
          <motion.form initial={{ opacity: 0, height: 0, marginTop: 0 }} animate={{ opacity: 1, height: 'auto', marginTop: 16 }} exit={{ opacity: 0, height: 0, marginTop: 0 }} className="search-box glass" style={{ overflow: 'hidden', flexWrap: 'wrap' }} onSubmit={handleMultiCitySearch}>
            <div style={{ width: '100%', fontSize: 13, color: '#64748b', marginBottom: 4 }}>
              One search only covers one place. This runs ONE niche across MANY cities automatically — leave Cities blank to
              sweep the built-in list of {CITIES.length} Maharashtra cities/regions, or type your own list for anywhere else.
            </div>
            <div className="input-group" style={{ minWidth: 200 }}>
              <label>Business Niche</label>
              <input type="text" list="niche-options" value={multiCityNiche} onChange={e => setMultiCityNiche(e.target.value)} placeholder="e.g. Dentist" />
            </div>
            <div className="input-group" style={{ flex: 2, minWidth: 240 }}>
              <label>Cities <span style={{ fontWeight: 400, color: '#94a3b8' }}>(optional — leave blank for the built-in list)</span></label>
              <textarea
                rows={2}
                value={multiCityCities}
                onChange={e => setMultiCityCities(e.target.value)}
                placeholder={"Leave blank for every built-in city, or type your own:\nDelhi\nBengaluru\nChennai"}
                style={{ width: '100%', padding: '10px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', fontFamily: 'inherit', fontSize: '14px', resize: 'vertical' }}
              />
            </div>
            <button type="submit" className="primary-btn" disabled={multiCitySearching} style={{ background: multiCitySearching ? '#94a3b8' : '#c026d3' }}>
              {multiCitySearching ? <Loader2 className="spin" /> : <Search />}
              {multiCitySearching ? 'Searching...' : 'Search Cities'}
            </button>
            {multiCitySearching && (
              <button type="button" onClick={() => { multiCityStopRef.current = true; }} style={{ padding: '0 16px', background: '#fee2e2', border: '1px solid #f87171', color: '#ef4444', borderRadius: '12px', cursor: 'pointer', fontWeight: 'bold' }}>
                Stop
              </button>
            )}
            {multiCityProgress && (
              <p style={{ width: '100%', margin: '4px 0 0', fontSize: 13, color: '#94a3b8' }}>
                {multiCityProgress.current}/{multiCityProgress.total}: {multiCityProgress.label}{searchProgressNote ? ` — ${searchProgressNote}` : ''}
              </p>
            )}
            {!multiCitySearching && multiCityLastResult && (
              <p style={{ width: '100%', margin: '4px 0 0', fontSize: 13, color: '#10b981' }}>{multiCityLastResult}</p>
            )}
          </motion.form>
        )}
      </AnimatePresence>

      {leads.length > 0 && (
        <div className="actions-bar" style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '16px', marginBottom: '20px', flexWrap: 'wrap' }}>
          <button
            className={`primary-btn ${isAutopilot ? 'danger' : ''}`}
            onClick={() => isAutopilot ? setIsAutopilot(false) : startAutopilot()}
            style={{ background: isAutopilot ? '#ef4444' : '#10b981', color: '#ffffff', border: 'none', padding: '12px 24px', fontSize: '16px', fontWeight: 'bold', borderRadius: '8px', boxShadow: '0 4px 6px rgba(0,0,0,0.1)' }}
          >
            <Activity className={isAutopilot ? 'spin' : ''} />
            {isAutopilot ? 'Stop Autopilot' : 'Start Autopilot (Audit All)'}
          </button>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <label htmlFor="lead-sort" style={{ fontSize: '13px', color: '#64748b', fontWeight: 600 }}>Sort by</label>
            <select
              id="lead-sort"
              value={leadSortKey}
              onChange={e => { setLeadSortKey(e.target.value); setLeadsPage(1); }}
              title="'Most reviews' and 'Highest rating' work on any fresh search. 'Highest capital' and 'Budget tier' need a lead to be audited first — unaudited leads sort last on those two, not as confirmed small."
              style={{ padding: '8px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', background: '#fff', fontSize: '14px' }}
            >
              <option value="default">Default (as found)</option>
              <option value="reviews">Most Google reviews</option>
              <option value="rating">Highest rating</option>
              <option value="tier">Budget tier (audited leads)</option>
              <option value="capital">Highest MCA capital (audited leads)</option>
            </select>
          </div>
        </div>
      )}

      <div className="leads-grid">
        <AnimatePresence>
          {sortedLeadIndices.slice((leadsPage - 1) * LEADS_PAGE_SIZE, leadsPage * LEADS_PAGE_SIZE).map((i) => {
          const lead = leads[i];
          return (
            <motion.div key={i} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: 0.9 }} className={`lead-card glass ${lead.auditState === 'rejected' ? 'rejected' : ''}`}>
              <div className="lead-header">
                <h3>{lead.Company}</h3>
                {lead.auditState === 'sent' && <span className="badge success"><Check size={14}/> Sent</span>}
                {lead.auditState === 'rejected' && <span className="badge danger"><X size={14}/> Rejected</span>}
              </div>

              {/* Cross-referenced against email_history, not just this
                  session's lead.auditState — catches a lead re-scraped in a
                  later session that was already emailed before. */}
              {lead.auditState !== 'sent' && sentWebsites[normaliseWebsiteKey(lead.Website)] && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: '0 0 10px', padding: '6px 10px', background: 'rgba(16,185,129,0.1)', border: '1px solid rgba(16,185,129,0.3)', borderRadius: '8px', fontSize: '12px' }}>
                  <Check size={14} color="#10b981" />
                  <span style={{ color: '#059669', fontWeight: 600 }}>
                    Already sent {sentWebsites[normaliseWebsiteKey(lead.Website)].timestamp}
                  </span>
                  <button
                    type="button"
                    onClick={() => goToSentEmail(sentWebsites[normaliseWebsiteKey(lead.Website)].id)}
                    style={{ marginLeft: 'auto', background: 'none', border: '1px solid #10b981', color: '#059669', borderRadius: '6px', padding: '3px 10px', fontSize: '12px', cursor: 'pointer', fontWeight: 600 }}
                  >
                    View sent email
                  </button>
                </div>
              )}

              {/* Same idea, one step earlier: a draft already exists for this
                  website from a PRIOR session/run — only shown when it isn't
                  already covered by the "Already sent" badge above, and not
                  once this session's own audit has finished ('done'), since
                  that's the very draft the badge would be pointing at and
                  the full draft is already showing on this same card. */}
              {lead.auditState !== 'sent' && lead.auditState !== 'done' && !sentWebsites[normaliseWebsiteKey(lead.Website)] && draftedWebsites[normaliseWebsiteKey(lead.Website)] && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: '0 0 10px', padding: '6px 10px', background: 'rgba(245,158,11,0.1)', border: '1px solid rgba(245,158,11,0.3)', borderRadius: '8px', fontSize: '12px' }}>
                  <FileEdit size={14} color="#f59e0b" />
                  <span style={{ color: '#b45309', fontWeight: 600 }}>
                    Draft already made {draftedWebsites[normaliseWebsiteKey(lead.Website)].timestamp}
                  </span>
                  <button
                    type="button"
                    onClick={() => goToDraft(draftedWebsites[normaliseWebsiteKey(lead.Website)].id)}
                    style={{ marginLeft: 'auto', background: 'none', border: '1px solid #f59e0b', color: '#b45309', borderRadius: '6px', padding: '3px 10px', fontSize: '12px', cursor: 'pointer', fontWeight: 600 }}
                  >
                    View draft
                  </button>
                </div>
              )}

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
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', alignItems: 'flex-start' }}>
                  <button className="audit-btn" onClick={() => handleAudit(i, false, false, false)}><Activity size={18} /> Generate AI Audit & Draft</button>
                  {/* Agriculture-sourced leads only — adds a few lines about
                      Metazyne/@agriusindia to the draft. Kept off the plain
                      button above so it's opt-in per draft, not automatic. */}
                  {lead.sector === 'agriculture' && (
                    <button className="audit-btn" style={{ background: '#166534' }} onClick={() => handleAudit(i, false, true, false)}>
                      <Sprout size={18} /> Generate for Agriculture
                    </button>
                  )}
                  {/* Textile-sourced leads only (added 2026-09-08) — adds a
                      line about alpinetexworld.com to the draft. Same
                      opt-in-per-draft gating as the agriculture line above. */}
                  {lead.sector === 'textile' && (
                    <button className="audit-btn" style={{ background: '#7c3aed' }} onClick={() => handleAudit(i, false, false, true)}>
                      <Shirt size={18} /> Generate for Textile
                    </button>
                  )}
                </div>
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
                      {/* Set only while this audit is genuinely waiting on
                          the shared global Playwright slot (analyzer/visuals.py's
                          semaphore, capacity 1) for another audit/search to
                          finish — distinguishes "actually stuck queued behind
                          someone else" from real in-progress work, which used
                          to look identical (reported live, two leads both
                          showing "Loading site" while only one was really
                          running). */}
                      {lead.auditProgress.note && (
                        <p style={{ margin: '2px 0 0', fontSize: '12px', color: '#b45309', fontWeight: 600 }}>
                          {lead.auditProgress.note}
                        </p>
                      )}
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
                  <button
                    type="button"
                    onClick={() => handleCancelAudit(i)}
                    style={{ background: 'none', border: '1px solid #cbd5e1', color: '#64748b', borderRadius: '6px', padding: '4px 14px', fontSize: '12px', cursor: 'pointer', fontWeight: 600 }}
                  >
                    <X size={12} style={{ verticalAlign: '-1px', marginRight: '4px' }} /> Cancel Audit
                  </button>
                </div>
              )}
              {lead.auditState === 'failed' && (
                <div className="auditing-state" style={{ flexDirection: 'column', gap: '8px' }}>
                  <p className="error-text">{lead.auditError || "Audit failed."}</p>
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
          <label>City (or a whole state)</label>
          <input
            type="text" list="city-options" value={agriCity} onChange={e => setAgriCity(e.target.value)}
            placeholder="e.g. Nashik, or Maharashtra for the whole state"
            title="A state name (e.g. Maharashtra) searches the whole state in one go for Maps/directory searches, same as the Dashboard tab."
          />
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

  // ---------------------------------------------------------------------
  // Social Media page
  // ---------------------------------------------------------------------
  const PLATFORM_META = {
    instagram: { label: 'Instagram', color: '#d6249f' },
    youtube: { label: 'YouTube', color: '#ff0000' },
    facebook: { label: 'Facebook', color: '#1877f2' },
    linkedin: { label: 'LinkedIn', color: '#0a66c2' },
  };
  const SEVERITY_COLOR = { high: '#ef4444', medium: '#f59e0b', low: '#64748b' };

  const fetchSocialDrafts = async () => {
    try {
      const res = await axios.get(`${API_BASE}/api/social/drafts`);
      setSocialDrafts(res.data.drafts || []);
    } catch (err) {
      console.error('Fetch social drafts failed:', err);
    }
  };

  const handleSocialSearch = async (e) => {
    if (e) e.preventDefault();
    if (!socialNiche.trim() || !socialCity.trim()) {
      alert('Enter a niche and a city.');
      return;
    }
    setSocialSearching(true);
    setSocialSearchStage('Starting...');
    setSocialBusinesses([]);
    try {
      const start = await axios.post(`${API_BASE}/api/social/search`, {
        niche: socialNiche.trim(), city: socialCity.trim(),
        limit: Number(socialLimit) || 10, async_mode: true,
      });
      const key = start.data.key;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise(r => setTimeout(r, 2000));
        const prog = await axios.get(`${API_BASE}/api/social/search/progress`, { params: { key } });
        if (prog.data.running && prog.data.stage) setSocialSearchStage(prog.data.stage);
        const res = await axios.get(`${API_BASE}/api/social/search/result`, { params: { key } });
        if (res.data.ready) {
          if (res.data.error) { alert(`Search failed: ${res.data.error}`); break; }
          setSocialBusinesses((res.data.businesses || []).map(b => ({ ...b, audit: null, auditing: false })));
          break;
        }
      }
    } catch (err) {
      console.error('Social search failed:', err);
      alert(`Social search failed: ${err.response?.data?.detail || err.message}`);
    } finally {
      setSocialSearching(false);
      setSocialSearchStage('');
    }
  };

  const handleSocialAudit = async (index) => {
    const biz = socialBusinesses[index];
    const handles = Object.fromEntries(
      Object.entries(biz.handles || {}).filter(([, v]) => !!v)
    );
    if (Object.keys(handles).length === 0) return;
    setSocialBusinesses(prev => prev.map((b, i) => i === index ? { ...b, auditing: true } : b));
    try {
      const start = await axios.post(`${API_BASE}/api/social/audit`, {
        company: biz.company, handles, async_mode: true,
      });
      const key = start.data.key;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise(r => setTimeout(r, 2000));
        const res = await axios.get(`${API_BASE}/api/social/audit/result`, { params: { key } });
        if (res.data.ready) {
          if (res.data.error) { alert(`Social audit failed: ${res.data.error}`); break; }
          setSocialBusinesses(prev => prev.map((b, i) => i === index ? { ...b, audit: res.data.results || {} } : b));
          break;
        }
      }
    } catch (err) {
      console.error('Social audit failed:', err);
      alert(`Social audit failed: ${err.response?.data?.detail || err.message}`);
    } finally {
      setSocialBusinesses(prev => prev.map((b, i) => i === index ? { ...b, auditing: false } : b));
    }
  };

  const setSocialCopy = (index, platform, channel, field, value) => {
    setSocialBusinesses(prev => prev.map((b, i) => {
      if (i !== index) return b;
      const audit = { ...b.audit };
      const plat = { ...audit[platform] };
      const outreach = { ...plat.outreach };
      outreach[channel] = { ...outreach[channel], [field]: value };
      plat.outreach = outreach;
      audit[platform] = plat;
      return { ...b, audit };
    }));
  };

  const saveSocialDraft = async (biz, platform, channel) => {
    const plat = biz.audit[platform];
    const copy = (plat.outreach && plat.outreach[channel]) || {};
    let target = '';
    if (channel === 'email') target = (prompt('Recipient email address:') || '').trim();
    else if (channel === 'whatsapp') target = biz.phone || '';
    else target = plat.profile?.handle || '';
    if (channel === 'email' && !target) return;
    try {
      await axios.post(`${API_BASE}/api/social/drafts`, {
        company: biz.company, platform, handle: plat.profile?.handle || '',
        profile_url: plat.profile?.url || '', channel, target,
        subject: copy.subject || '', body: copy.body || '',
        issues: plat.issues || [],
      });
      fetchSocialDrafts();
      alert('Saved to social drafts.');
    } catch (err) {
      alert(`Save failed: ${err.response?.data?.detail || err.message}`);
    }
  };

  const openSocialChannel = (biz, platform, channel) => {
    const plat = biz.audit[platform];
    const copy = (plat.outreach && plat.outreach[channel]) || {};
    if (channel === 'email') {
      const to = (prompt('Recipient email address:') || '').trim();
      if (!to) return;
      window.open(`mailto:${to}?subject=${encodeURIComponent(copy.subject || '')}&body=${encodeURIComponent(copy.body || '')}`);
    } else if (channel === 'whatsapp') {
      const digits = (biz.phone || '').replace(/\D/g, '');
      if (!digits) { alert('No phone number for this business.'); return; }
      window.open(`https://wa.me/${digits}?text=${encodeURIComponent(copy.body || '')}`);
    } else {
      navigator.clipboard?.writeText(copy.body || '');
      window.open(`https://instagram.com/${plat.profile?.handle || ''}`);
    }
  };

  const deleteSocialDraft = async (id) => {
    try {
      await axios.delete(`${API_BASE}/api/social/drafts/${id}`);
      setSocialDrafts(prev => prev.filter(d => d.id !== id));
    } catch (err) {
      alert(`Delete failed: ${err.response?.data?.detail || err.message}`);
    }
  };

  const sendSocialEmailDraft = async (d) => {
    if (d.channel !== 'email') return;
    if (!window.confirm(`Send this email to ${d.target}?`)) return;
    try {
      const res = await axios.post(`${API_BASE}/api/social/send`, {
        company: d.company, target: d.target, subject: d.subject, body: d.body,
      });
      if (res.data.sent) {
        alert('Sent.');
        deleteSocialDraft(d.id);
      } else {
        alert(`Send failed: ${res.data.error || 'unknown'}`);
      }
    } catch (err) {
      alert(`Send failed: ${err.response?.data?.detail || err.message}`);
    }
  };

  const openSocialDraftChannel = (d) => {
    if (d.channel === 'email') {
      window.open(`mailto:${d.target}?subject=${encodeURIComponent(d.subject || '')}&body=${encodeURIComponent(d.body || '')}`);
    } else if (d.channel === 'whatsapp') {
      const digits = (d.target || '').replace(/\D/g, '');
      if (digits) window.open(`https://wa.me/${digits}?text=${encodeURIComponent(d.body || '')}`);
    } else {
      navigator.clipboard?.writeText(d.body || '');
      window.open(`https://instagram.com/${d.handle || ''}`);
    }
  };

  const renderSocialAuditResult = (biz, index) => (
    <div style={{ marginTop: '14px', borderTop: '1px solid rgba(148,163,184,0.2)', paddingTop: '14px' }}>
      {Object.entries(biz.audit).map(([platform, data]) => {
        const chanKey = `${index}-${platform}`;
        const active = socialActiveChannel[chanKey] || 'email';
        const meta = PLATFORM_META[platform] || { label: platform, color: '#64748b' };
        return (
          <div key={platform} style={{ marginBottom: '18px' }}>
            <div style={{ fontWeight: 700, color: meta.color, marginBottom: '6px' }}>{meta.label}</div>
            {!data.profile && <p style={{ color: '#94a3b8', fontSize: '13px' }}>{data.note || 'Could not analyze this profile.'}</p>}
            {data.profile && (
              <>
                <p style={{ fontSize: '13px', color: '#475569', margin: '2px 0' }}>
                  {Number(data.profile.followers || 0).toLocaleString()} followers
                  {data.profile.posting_frequency ? ` · ${data.profile.posting_frequency}` : ''}
                  {data.profile.avg_engagement_rate ? ` · ${data.profile.avg_engagement_rate}% engagement` : ''}
                </p>
                {data.note && <p style={{ color: '#94a3b8', fontSize: '12px' }}>{data.note}</p>}
                {(data.issues || []).length > 0 && (
                  <ul style={{ margin: '8px 0', paddingLeft: '18px' }}>
                    {data.issues.map((iss, k) => (
                      <li key={k} style={{ fontSize: '13px', margin: '3px 0' }}>
                        <span style={{ color: SEVERITY_COLOR[iss.severity], fontWeight: 700 }}>{iss.label}</span>
                        {': '}{iss.detail}
                      </li>
                    ))}
                  </ul>
                )}
                {data.outreach && Object.keys(data.outreach).length > 0 && (
                  <div style={{ marginTop: '10px' }}>
                    <div style={{ display: 'flex', gap: '6px', marginBottom: '6px' }}>
                      {['email', 'dm', 'whatsapp'].map(ch => (
                        <button key={ch} type="button"
                          onClick={() => setSocialActiveChannel(prev => ({ ...prev, [chanKey]: ch }))}
                          style={{
                            fontSize: '12px', padding: '4px 12px', borderRadius: '8px', cursor: 'pointer',
                            border: '1px solid #cbd5e1',
                            background: active === ch ? '#10b981' : 'transparent',
                            color: active === ch ? '#fff' : '#64748b', fontWeight: 600,
                          }}>{ch.toUpperCase()}</button>
                      ))}
                    </div>
                    {active === 'email' && (
                      <input
                        value={data.outreach.email?.subject || ''}
                        onChange={e => setSocialCopy(index, platform, 'email', 'subject', e.target.value)}
                        placeholder="Subject"
                        style={{ width: '100%', padding: '8px', marginBottom: '6px', fontWeight: 600 }}
                      />
                    )}
                    <textarea
                      value={data.outreach[active]?.body || ''}
                      onChange={e => setSocialCopy(index, platform, active, 'body', e.target.value)}
                      rows={active === 'email' ? 7 : 4}
                      style={{ width: '100%', padding: '8px' }}
                    />
                    <div style={{ display: 'flex', gap: '8px', marginTop: '6px', flexWrap: 'wrap' }}>
                      <button type="button" onClick={() => navigator.clipboard?.writeText(data.outreach[active]?.body || '')}
                        style={{ fontSize: '12px', padding: '5px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Copy</button>
                      <button type="button" onClick={() => openSocialChannel(biz, platform, active)}
                        style={{ fontSize: '12px', padding: '5px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Open {active}</button>
                      <button type="button" onClick={() => saveSocialDraft(biz, platform, active)}
                        style={{ fontSize: '12px', padding: '5px 12px', borderRadius: '8px', border: 'none', background: '#10b981', color: '#fff', fontWeight: 600, cursor: 'pointer' }}>Save draft</button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );

  const renderSocial = () => (
    <div className="glass" style={{ padding: '24px' }}>
      <h2 style={{ margin: 0 }}>Social Media Outreach</h2>
      <p style={{ color: '#94a3b8', margin: '8px 0 16px' }}>
        Find a business, audit its social presence, and reach out by email, DM or WhatsApp.
      </p>

      <form onSubmit={handleSocialSearch} style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginBottom: '20px' }}>
        <input value={socialNiche} onChange={e => setSocialNiche(e.target.value)} placeholder="Business niche (e.g. candle shop)" style={{ flex: '1 1 220px', padding: '10px' }} />
        <input value={socialCity} onChange={e => setSocialCity(e.target.value)} placeholder="City" style={{ flex: '1 1 160px', padding: '10px' }} />
        <input type="number" min="1" max="30" value={socialLimit} onChange={e => setSocialLimit(e.target.value)} style={{ width: '80px', padding: '10px' }} />
        <button type="submit" disabled={socialSearching} className="primary-btn" style={{ background: '#10b981', color: '#fff', border: 'none', padding: '10px 20px', borderRadius: '8px', fontWeight: 'bold' }}>
          {socialSearching ? 'Searching...' : 'Find Businesses'}
        </button>
      </form>

      {socialSearching && socialSearchStage && (
        <p style={{ color: '#64748b', fontSize: '13px' }}>{socialSearchStage}</p>
      )}

      <div className="leads-grid" style={{ gridTemplateColumns: '1fr' }}>
        {socialBusinesses.map((b, i) => (
          <div key={`${b.company}-${i}`} className="lead-card glass">
            <div className="lead-header"><h3>{b.company}</h3></div>
            <div className="lead-details">
              {b.website && <p><strong>Site:</strong> <a href={b.website} target="_blank" rel="noreferrer">{b.website}</a></p>}
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', margin: '8px 0' }}>
                {Object.keys(PLATFORM_META).map(p => {
                  const has = !!(b.handles && b.handles[p]);
                  return (
                    <span key={p} style={{
                      fontSize: '12px', fontWeight: 600, padding: '3px 10px', borderRadius: '999px',
                      border: `1px solid ${has ? PLATFORM_META[p].color : '#cbd5e1'}`,
                      color: has ? PLATFORM_META[p].color : '#94a3b8',
                      background: has ? `${PLATFORM_META[p].color}14` : 'transparent',
                    }}>{PLATFORM_META[p].label}{has ? '' : ' (none)'}</span>
                  );
                })}
              </div>
              <button
                type="button"
                disabled={b.auditing || !Object.values(b.handles || {}).some(Boolean)}
                onClick={() => handleSocialAudit(i)}
                className="send-btn"
                style={{ marginTop: '8px' }}
              >
                {b.auditing ? 'Auditing socials...' : 'Audit socials'}
              </button>
            </div>
            {b.audit && renderSocialAuditResult(b, i)}
          </div>
        ))}
      </div>

      <div style={{ marginTop: '30px', borderTop: '1px solid rgba(148,163,184,0.2)', paddingTop: '18px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '10px' }}>
          <h3 style={{ margin: 0 }}>Social Drafts ({socialDrafts.length})</h3>
          <GroupBySelector value={socialDraftsGroupBy} onChange={setSocialDraftsGroupBy} />
        </div>
        {groupByPeriod(socialDrafts, socialDraftsGroupBy).map(group => (
          <div key={group.key}>
            {group.label && <h4 style={{ margin: '16px 0 8px', color: '#94a3b8', fontSize: '13px' }}>{group.label} ({group.items.length})</h4>}
            {group.items.map(d => (
              <div key={d.id} className="lead-card glass" style={{ marginBottom: '10px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
                  <strong>{d.company}</strong>
                  <span style={{ fontSize: '12px', color: '#64748b' }}>{(PLATFORM_META[d.platform]?.label || d.platform)} · {String(d.channel || '').toUpperCase()}</span>
                </div>
                {d.subject && <p style={{ fontWeight: 600, margin: '6px 0 2px' }}>{d.subject}</p>}
                <p style={{ whiteSpace: 'pre-wrap', fontSize: '13px', color: '#475569', margin: '4px 0' }}>{d.body}</p>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '6px' }}>
                  <button type="button" onClick={() => navigator.clipboard?.writeText(d.body || '')} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Copy</button>
                  <button type="button" onClick={() => openSocialDraftChannel(d)} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Open {d.channel}</button>
                  {d.channel === 'email' && (
                    <button type="button" onClick={() => sendSocialEmailDraft(d)} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: 'none', background: '#10b981', color: '#fff', fontWeight: 600, cursor: 'pointer' }}>Send</button>
                  )}
                  <button type="button" onClick={() => deleteSocialDraft(d.id)} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: '1px solid #fca5a5', color: '#ef4444', cursor: 'pointer' }}>Delete</button>
                </div>
              </div>
            ))}
          </div>
        ))}
        {socialDrafts.length === 0 && <p style={{ color: '#94a3b8', fontSize: '13px' }}>No social drafts yet.</p>}
      </div>
    </div>
  );

  const renderDrafts = () => {
    const draftGroups = groupByPeriod(drafts, draftsGroupBy);
    const draftsInView = draftGroups.flatMap(g => g.items);
    return (
    <div className="glass" style={{ padding: '24px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '12px' }}>
        <div>
          <h2 style={{ margin: 0 }}><FileEdit style={{display:'inline', marginRight: '8px', verticalAlign: 'middle'}}/> Saved Drafts</h2>
          <p style={{color: '#94a3b8', margin: '8px 0 0'}}>AI-generated audits ready for your review and approval.</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
          {sendAllRunning && sendAllGroupKey === '__all__' ? (
            <>
              <span style={{ fontSize: '13px', color: '#64748b', fontWeight: 600 }}>
                Sending {sendAllProgress.done}/{sendAllProgress.total}…
                {sendAllNote && <span style={{ color: '#b45309', fontWeight: 500 }}> · {sendAllNote}</span>}
              </span>
              <button
                type="button"
                onClick={() => { sendAllStopRef.current = true; }}
                style={{ padding: '8px 16px', background: '#fee2e2', border: '1px solid #f87171', color: '#ef4444', borderRadius: '10px', cursor: 'pointer', fontWeight: 'bold' }}
              >
                Stop
              </button>
            </>
          ) : (
            draftsInView.length > 0 && (
              <button
                type="button"
                disabled={sendAllRunning}
                onClick={() => handleSendAll(draftsInView)}
                style={{ padding: '8px 16px', background: sendAllRunning ? '#6ee7b7' : '#10b981', border: 'none', color: '#ffffff', borderRadius: '10px', cursor: sendAllRunning ? 'not-allowed' : 'pointer', fontWeight: 'bold', display: 'inline-flex', alignItems: 'center', gap: '6px' }}
              >
                <Send size={16} /> Send All ({draftsInView.length})
              </button>
            )
          )}
          <GroupBySelector value={draftsGroupBy} onChange={setDraftsGroupBy} />
        </div>
      </div>

      <div className="leads-grid" style={{ gridTemplateColumns: '1fr', marginTop: '20px' }}>
        <AnimatePresence>
          {draftGroups.map(group => {
            const groupSendable = group.items.filter(d => d.target_email).length;
            const groupSending = sendAllRunning && sendAllGroupKey === group.key;
            return (
            <div key={group.key}>
              {group.label && (
                <h4 style={{ margin: '20px 0 10px', color: '#94a3b8', fontSize: '14px', fontWeight: 600, borderBottom: '1px solid rgba(148,163,184,0.15)', paddingBottom: '6px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
                  <span>{group.label} <span style={{ color: '#64748b', fontWeight: 400 }}>({group.items.length})</span></span>
                  {groupSending ? (
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontSize: '12px', color: '#64748b', fontWeight: 600 }}>
                        Sending {sendAllProgress.done}/{sendAllProgress.total}…
                        {sendAllNote && <span style={{ color: '#b45309', fontWeight: 500 }}> · {sendAllNote}</span>}
                      </span>
                      <button
                        type="button"
                        onClick={() => { sendAllStopRef.current = true; }}
                        style={{ padding: '4px 12px', background: '#fee2e2', border: '1px solid #f87171', color: '#ef4444', borderRadius: '8px', cursor: 'pointer', fontWeight: 700, fontSize: '12px' }}
                      >
                        Stop
                      </button>
                    </span>
                  ) : (
                    groupSendable > 0 && (
                      <button
                        type="button"
                        disabled={sendAllRunning}
                        onClick={() => handleSendAll(group.items, group.key, group.label)}
                        style={{ padding: '4px 12px', background: sendAllRunning ? '#6ee7b7' : '#10b981', border: 'none', color: '#ffffff', borderRadius: '8px', cursor: sendAllRunning ? 'not-allowed' : 'pointer', fontWeight: 700, fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '5px' }}
                      >
                        <Send size={13} /> Send {group.label} ({groupSendable})
                      </button>
                    )
                  )}
                </h4>
              )}
              {group.items.map((draft) => {
                const i = drafts.findIndex(d => d.id === draft.id);
                return (
            <motion.div key={draft.id} id={`draft-${draft.id}`} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: 0.9 }} className="lead-card glass" style={draftScrollTarget === draft.id ? { boxShadow: '0 0 0 2px rgba(245,158,11,0.6)' } : undefined}>
              <div className="lead-header">
                <h3>{draft.company}</h3>
                <span className="badge" style={{background: 'rgba(245, 158, 11, 0.2)', color: '#f59e0b', border: '1px solid rgba(245, 158, 11, 0.4)'}}>Draft</span>
              </div>
              <div className="lead-details">
                <p><strong>URL:</strong> <a href={draft.website} target="_blank" rel="noreferrer">{draft.website}</a></p>
                <p><strong>To:</strong> {draft.target_email || 'Missing email'}</p>
              </div>

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
                );
              })}
            </div>
            );
          })}
        </AnimatePresence>
        {drafts.length === 0 && <p style={{textAlign: 'center', color: '#64748b', padding: '40px 0'}}>No saved drafts.</p>}
      </div>
    </div>
    );
  };

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

  // Reads the actual sent email (via /api/generate-followup, which reads
  // it server-side) and drafts a follow-up in the same voice, rather than
  // scheduler.py's automated sequence's hardcoded, name-and-stage-only copy.
  const handleGenerateFollowup = async (logId) => {
    const stage = followupDrafts[logId]?.nextStage || 1;
    setFollowupDrafts(prev => ({ ...prev, [logId]: { ...prev[logId], loading: true } }));
    try {
      const res = await axios.post(`${API_BASE}/api/generate-followup`, { history_id: logId, stage });
      setFollowupDrafts(prev => ({
        ...prev,
        [logId]: { ...prev[logId], loading: false, subject: res.data.subject, body: res.data.body, to_email: res.data.to_email, nextStage: stage },
      }));
    } catch (err) {
      console.error('Generate follow-up failed:', err);
      alert(`Could not draft a follow-up: ${err.response?.data?.detail || err.message}`);
      setFollowupDrafts(prev => ({ ...prev, [logId]: { ...prev[logId], loading: false } }));
    }
  };

  const handleEditFollowupField = (logId, field, value) => {
    setFollowupDrafts(prev => ({ ...prev, [logId]: { ...prev[logId], [field]: value } }));
  };

  const handleSendFollowup = async (logId) => {
    const draft = followupDrafts[logId];
    if (!draft) return;
    setFollowupDrafts(prev => ({ ...prev, [logId]: { ...prev[logId], sending: true } }));
    try {
      await axios.post(`${API_BASE}/api/send-followup`, {
        history_id: logId, subject: draft.subject, body: draft.body,
      });
      alert('Follow-up sent.');
      // Clear the draft and advance to the final-stage wording for next
      // time, matching the 3-email sequence's own stage 1 -> stage 2 shape.
      setFollowupDrafts(prev => ({ ...prev, [logId]: { nextStage: 2 } }));
      const res = await axios.get(`${API_BASE}/api/history?t=${Date.now()}`);
      setHistoryLogs(res.data.history);
    } catch (err) {
      console.error('Send follow-up failed:', err);
      alert(`Could not send the follow-up: ${err.response?.data?.detail || err.message}`);
      setFollowupDrafts(prev => ({ ...prev, [logId]: { ...prev[logId], sending: false } }));
    }
  };

  const handleDiscardFollowup = (logId) => {
    setFollowupDrafts(prev => ({ ...prev, [logId]: { nextStage: prev[logId]?.nextStage || 1 } }));
  };

  const renderHistory = () => {
    const historyGroups = groupByPeriod(historyLogs, historyGroupBy);
    return (
    <div className="glass" style={{ padding: '24px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
        <h2 style={{ margin: 0 }}><Clock style={{display:'inline', marginRight: '8px', verticalAlign: 'middle'}}/> Email Sent History</h2>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <GroupBySelector value={historyGroupBy} onChange={setHistoryGroupBy} />
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
        {historyGroups.map(group => (
        <div key={group.key}>
          {group.label && (
            <h4 style={{ margin: '4px 0 12px', color: '#94a3b8', fontSize: '14px', fontWeight: 600, borderBottom: '1px solid rgba(148,163,184,0.15)', paddingBottom: '6px' }}>
              {group.label} <span style={{ color: '#64748b', fontWeight: 400 }}>({group.items.length})</span>
            </h4>
          )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {group.items.map(log => (
          <div key={log.id} id={`history-${log.id}`} style={{ background: historyScrollTarget === log.id ? 'rgba(16,185,129,0.08)' : 'rgba(255,255,255,0.02)', padding: '16px', borderRadius: '8px', border: historyScrollTarget === log.id ? '1px solid rgba(16,185,129,0.5)' : '1px solid rgba(255,255,255,0.1)' }}>
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
                <button
                  style={{ display: 'block', marginLeft: 'auto', background: 'none', border: 'none', color: '#8b5cf6', cursor: followupDrafts[log.id]?.loading ? 'default' : 'pointer', fontSize: '13px', padding: '4px 0' }}
                  onClick={() => handleGenerateFollowup(log.id)}
                  disabled={followupDrafts[log.id]?.loading}
                  title="Draft a follow-up that reads this exact email, via AI"
                >
                  {followupDrafts[log.id]?.loading
                    ? 'Drafting...'
                    : (followupDrafts[log.id]?.nextStage === 2 ? 'Generate Final Follow-up' : 'Generate Follow-up')}
                </button>
              </div>
            </div>

            {expandedEmail === log.id && (
              <div style={{ marginTop: '16px', padding: '16px', background: 'rgba(0,0,0,0.2)', borderRadius: '6px' }}>
                <p style={{ margin: '0 0 12px 0', fontSize: '14px', fontWeight: 'bold' }}>Subject: {log.subject}</p>
                <p style={{ margin: 0, fontSize: '13px', whiteSpace: 'pre-wrap', lineHeight: '1.6' }}>{log.body}</p>
              </div>
            )}

            {followupDrafts[log.id]?.subject !== undefined && (
              <div style={{ marginTop: '16px', padding: '16px', background: 'rgba(139,92,246,0.08)', border: '1px solid rgba(139,92,246,0.3)', borderRadius: '6px' }}>
                <p style={{ margin: '0 0 10px 0', fontSize: '13px', fontWeight: 'bold', color: '#8b5cf6' }}>
                  Follow-up draft — to {followupDrafts[log.id].to_email || log.target_email}
                </p>
                <input
                  className="subject-editor"
                  style={{ width: '100%', marginBottom: '8px', padding: '8px', borderRadius: '6px', border: '1px solid rgba(255,255,255,0.15)', background: 'rgba(0,0,0,0.2)', color: '#e2e8f0', fontSize: '13px' }}
                  value={followupDrafts[log.id].subject}
                  onChange={(e) => handleEditFollowupField(log.id, 'subject', e.target.value)}
                />
                <textarea
                  style={{ width: '100%', minHeight: '120px', padding: '8px', borderRadius: '6px', border: '1px solid rgba(255,255,255,0.15)', background: 'rgba(0,0,0,0.2)', color: '#e2e8f0', fontSize: '13px', fontFamily: 'inherit', resize: 'vertical' }}
                  value={followupDrafts[log.id].body}
                  onChange={(e) => handleEditFollowupField(log.id, 'body', e.target.value)}
                />
                <div style={{ display: 'flex', gap: '8px', marginTop: '10px' }}>
                  <button
                    onClick={() => handleSendFollowup(log.id)}
                    disabled={followupDrafts[log.id].sending}
                    style={{ padding: '8px 16px', background: '#8b5cf6', border: 'none', borderRadius: '6px', color: '#fff', cursor: followupDrafts[log.id].sending ? 'default' : 'pointer', fontSize: '13px', fontWeight: 'bold' }}
                  >
                    {followupDrafts[log.id].sending ? 'Sending...' : 'Send Follow-up'}
                  </button>
                  <button
                    onClick={() => handleDiscardFollowup(log.id)}
                    disabled={followupDrafts[log.id].sending}
                    style={{ padding: '8px 16px', background: 'none', border: '1px solid rgba(255,255,255,0.2)', borderRadius: '6px', color: '#94a3b8', cursor: 'pointer', fontSize: '13px' }}
                  >
                    Discard
                  </button>
                </div>
              </div>
            )}
          </div>
          ))}
          </div>
        </div>
        ))}
        {historyLogs.length === 0 && <p style={{textAlign: 'center', color: '#64748b', padding: '40px 0'}}>No emails sent yet.</p>}
      </div>
    </div>
    );
  };

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
            onClick={() => setCurrentView('social')}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', background: currentView === 'social' ? 'rgba(255,255,255,0.15)' : 'transparent', border: 'none', borderRadius: '8px', color: currentView === 'social' ? '#fff' : '#94a3b8', cursor: 'pointer', fontSize: '15px', fontWeight: currentView === 'social' ? 'bold' : 'normal', transition: 'all 0.2s' }}
          >
            <MessageCircle size={18} /> Social
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
        {currentView === 'social' && renderSocial()}
        {currentView === 'drafts' && renderDrafts()}
        {currentView === 'history' && renderHistory()}
        {currentView === 'cost' && renderCost()}
      </main>
    </div>
  );
}

export default App;
