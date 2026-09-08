// Default company list for the Dashboard's "Search MNCs" panel, used when
// the Company Names box is left blank — "just give the location" (requested
// 2026-09-08). No free API can filter Google Places results down to
// "is this a multinational" directly, so this is a curated, well-known list
// instead: major global and India-headquartered MNCs with a real local
// office presence, spanning IT/consulting, FMCG, banking, electronics,
// autos and logistics so a search isn't dominated by one industry. Each
// name gets its own exact-listing lookup (limit=1) against whatever city
// the user types, same mechanism as typing a custom list. Not exhaustive —
// add to it as gaps show up in real use, same maintenance model as
// AGRI_NICHES/NICHES.
export const MNC_NAMES = [
  'Google', 'Microsoft', 'Amazon', 'Meta', 'IBM', 'Oracle', 'SAP', 'Cisco',
  'Intel', 'Dell', 'HP', 'Adobe', 'Salesforce', 'Accenture', 'Cognizant',
  'Capgemini', 'Deloitte', 'EY', 'KPMG', 'PwC',
  'Tata Consultancy Services', 'Infosys', 'Wipro', 'HCLTech',
  'Samsung', 'LG Electronics', 'Sony',
  'Unilever', 'Nestle', 'Procter & Gamble', 'Coca-Cola', 'PepsiCo',
  'HDFC Bank', 'ICICI Bank', 'HSBC', 'Standard Chartered', 'Citibank',
  'Vodafone', 'Siemens', 'General Electric', 'Bosch', 'Schneider Electric',
  'DHL', 'FedEx', 'Maersk',
  'Ford', 'Toyota', 'Honda',
];
