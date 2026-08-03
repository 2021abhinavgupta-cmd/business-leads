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

// Indian metros and tier-2 cities, since that's where the SES sending domain
// and the existing Maps queries are focused.
export const CITIES = [
  // Maharashtra
  'Mumbai', 'Navi Mumbai', 'Thane', 'Pune', 'Nagpur', 'Nashik', 'Kolhapur',
  'Solapur', 'Chhatrapati Sambhajinagar', 'Amravati', 'Nanded', 'Sangli',
  // Metros
  'Delhi', 'New Delhi', 'Gurgaon', 'Noida', 'Faridabad', 'Ghaziabad',
  'Bangalore', 'Hyderabad', 'Chennai', 'Kolkata', 'Ahmedabad', 'Surat',
  // North
  'Jaipur', 'Lucknow', 'Kanpur', 'Chandigarh', 'Ludhiana', 'Amritsar',
  'Jalandhar', 'Dehradun', 'Agra', 'Varanasi', 'Meerut', 'Jodhpur', 'Udaipur',
  'Srinagar', 'Jammu', 'Shimla',
  // West
  'Vadodara', 'Rajkot', 'Bhavnagar', 'Jamnagar', 'Gandhinagar', 'Goa',
  'Panaji', 'Indore', 'Bhopal', 'Gwalior', 'Jabalpur', 'Raipur',
  // South
  'Coimbatore', 'Madurai', 'Tiruchirappalli', 'Salem', 'Kochi',
  'Thiruvananthapuram', 'Kozhikode', 'Thrissur', 'Mysore', 'Mangalore',
  'Hubli', 'Belgaum', 'Vijayawada', 'Visakhapatnam', 'Guntur', 'Warangal',
  'Puducherry',
  // East
  'Bhubaneswar', 'Cuttack', 'Patna', 'Ranchi', 'Jamshedpur', 'Dhanbad',
  'Guwahati', 'Siliguri', 'Durgapur', 'Asansol', 'Shillong', 'Agartala',
];
