// Suggestion lists for the lead-search form. These are `datalist` options,
// so they're suggestions and not a whitelist — anything typed still works.
// Kept out of App.jsx purely because the lists are long enough to bury the
// component otherwise.

// Local service businesses dominate this list on purpose: they're the ones
// that reliably have a weak website, a Google Business listing, and an owner
// who answers their own email — which is exactly the lead this tool audits
// well. Big-brand niches tend to return chain branches that dedupe down to
// one domain (see GoogleMapsScraper._deduplicate).
export const NICHES = [
  // Marketing & creative
  'Digital Marketing Agency', 'SEO Agency', 'Advertising Agency', 'Branding Agency',
  'Graphic Design Studio', 'Video Production Company', 'Photography Studio',
  'Printing Press', 'Event Management Company', 'Wedding Planner',
  // Tech
  'Software Development', 'IT Services', 'Web Design Company', 'Mobile App Developer',
  'Cyber Security Firm', 'Computer Repair Shop',
  // Health & wellness
  'Dental Clinic', 'Dermatology Clinic', 'Physiotherapy Clinic', 'Eye Clinic',
  'Veterinary Clinic', 'Diagnostic Lab', 'Ayurvedic Clinic', 'Homeopathy Clinic',
  'Nutritionist', 'Mental Health Clinic', 'IVF Clinic', 'Pharmacy',
  // Fitness & beauty
  'Gym', 'Yoga Studio', 'Pilates Studio', 'CrossFit Box', 'Dance Studio',
  'Salon', 'Spa', 'Barber Shop', 'Nail Salon', 'Tattoo Studio', 'Skin Clinic',
  // Professional services
  'Law Firm', 'Accounting Firm', 'Chartered Accountant', 'Tax Consultant',
  'Insurance Agency', 'Financial Advisor', 'Recruitment Agency', 'HR Consultancy',
  'Management Consultant', 'Architecture Firm', 'Interior Designer',
  'Civil Engineer', 'Notary', 'Immigration Consultant',
  // Property & construction
  'Real Estate Agency', 'Property Management', 'Construction Company',
  'Builder and Developer', 'Modular Kitchen Showroom', 'Furniture Store',
  'Home Decor Store', 'Landscaping Service',
  // Home & trade services
  'Plumbing Services', 'Electrician', 'HVAC Contractor', 'Painting Contractor',
  'Carpenter', 'Pest Control Service', 'Cleaning Service', 'Packers and Movers',
  'Solar Panel Installer', 'Security Systems Installer', 'Borewell Service',
  // Food & hospitality
  'Restaurant', 'Cafe', 'Bakery', 'Cloud Kitchen', 'Catering Service',
  'Hotel', 'Resort', 'Banquet Hall', 'Bar and Lounge', 'Sweet Shop',
  // Retail
  'Boutique', 'Clothing Store', 'Jewellery Store', 'Optical Store',
  'Electronics Store', 'Mobile Phone Store', 'Sports Shop', 'Book Store',
  'Pet Store', 'Florist', 'Gift Shop', 'Supermarket', 'Organic Store',
  // Automotive
  'Car Dealership', 'Car Service Center', 'Car Detailing', 'Bike Showroom',
  'Tyre Shop', 'Driving School', 'Car Rental',
  // Education
  'Coaching Classes', 'Play School', 'Preschool', 'International School',
  'Music School', 'Language Institute', 'Computer Training Institute',
  'Study Abroad Consultant', 'Art Classes', 'Skill Development Institute',
  // Travel & logistics
  'Travel Agency', 'Tour Operator', 'Visa Consultant', 'Logistics Company',
  'Courier Service', 'Warehouse Service',
  // B2B / industrial
  'Manufacturing Company', 'Wholesale Distributor', 'Export Import Company',
  'Industrial Equipment Supplier', 'Chemical Supplier', 'Packaging Company',
  'Textile Manufacturer', 'Machine Shop',
];

// Maharashtra only. The rest of India was removed deliberately: these are
// suggestions for a human picking a search area, and a list that spans the
// whole country makes the operator scroll past 70 irrelevant cities to reach
// the dozen actually worked. Still a `datalist`, so any city typed by hand
// continues to work exactly as before — this narrows the suggestions, not
// what the scraper accepts.
//
// Ordered roughly by lead density (MMR and Pune first), then the rest of the
// state grouped by region so a nearby city is easy to find.
export const CITIES = [
  // Mumbai Metropolitan Region
  'Mumbai', 'Navi Mumbai', 'Thane', 'Kalyan', 'Dombivli', 'Vasai', 'Virar',
  'Mira Bhayandar', 'Bhiwandi', 'Ulhasnagar', 'Ambernath', 'Badlapur',
  'Panvel', 'Karjat', 'Alibag',
  // Pune region
  'Pune', 'Pimpri Chinchwad', 'Hinjewadi', 'Wakad', 'Baner', 'Kharadi',
  'Hadapsar', 'Talegaon', 'Chakan', 'Lonavala', 'Baramati',
  // North Maharashtra
  'Nashik', 'Malegaon', 'Jalgaon', 'Dhule', 'Nandurbar', 'Ahmednagar',
  'Shirdi', 'Sinnar',
  // Marathwada
  'Chhatrapati Sambhajinagar', 'Nanded', 'Latur', 'Dharashiv', 'Beed',
  'Jalna', 'Parbhani', 'Hingoli',
  // Vidarbha
  'Nagpur', 'Amravati', 'Akola', 'Chandrapur', 'Yavatmal', 'Wardha',
  'Gondia', 'Bhandara', 'Buldhana', 'Washim', 'Gadchiroli',
  // Western Maharashtra / Konkan
  'Kolhapur', 'Sangli', 'Satara', 'Solapur', 'Ichalkaranji', 'Miraj',
  'Karad', 'Ratnagiri', 'Chiplun', 'Sindhudurg', 'Sawantwadi',
];
