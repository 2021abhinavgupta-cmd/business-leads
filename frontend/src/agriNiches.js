// Preset niches for the Agriculture tab — click-to-search, unlike the free-text
// datalist in searchOptions.js. Curated for Maharashtra's farming belts
// (Nashik, Ahmednagar, Kolhapur, Solapur, Jalgaon etc — already in CITIES).
//
// Expect thinner yield per search than other niches: GoogleMapsScraper skips
// listings with no website, and a lot of agri dealers/farms genuinely don't
// have one. That's why this tab also offers an IndiaMART search (see
// IndiaMartDorkScraper) as a second source for exactly those leads.
export const AGRI_NICHES = [
  'Agricultural Equipment Dealer',
  'Tractor Dealership',
  'Fertilizer and Pesticide Shop',
  'Seed Store',
  'Dairy Farm',
  'Poultry Farm',
  'Cold Storage Facility',
  'Irrigation Equipment Supplier',
  'Agri-Tech Startup',
  'Food Processing Unit',
  'Grain Warehouse',
  'Plant Nursery',
  'Agricultural Export Company',
  'Organic Farming Consultant',
  'Farm Equipment Rental',
  'Agricultural Cooperative Society',
  'Veterinary Clinic for Livestock',
  'Sugar Mill',
];
