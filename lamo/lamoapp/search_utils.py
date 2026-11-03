"""
LAMO Intelligent Search & Suggestions Engine
Provides typo-tolerant, fuzzy matching, phonetic similarity, and category aliases
so that user searches always return relevant services even with misspellings.
"""

import difflib
import re

# Comprehensive dictionary mapping categories to canonical titles, icons, and common user aliases/misspellings/Manglish words
CATEGORY_ALIASES = {
    'electrical': {
        'canonical': 'Electrician',
        'category_label': 'Electrical',
        'category_key': 'electrical',
        'icon': 'bi bi-lightning-charge-fill',
        'badge_bg': '#e3f2fd',
        'badge_color': '#1565c0',
        'terms': [
            'electrician', 'electrical', 'electric', 'elect', 'current', 'wire', 'wiring',
            'switch', 'plug', 'socket', 'fuse', 'inverter', 'motor', 'pump', 'short circuit',
            'fan', 'light', 'tube light', 'bulb', 'ac power', 'breaker', 'mcb', 'generator',
            'smart tv', 'electronics', 'elctrician', 'electrcian', 'electrishian', 'electritian',
            'electrecian', 'electic', 'electical', 'elec', 'karanr', 'karent', 'karan',
            'current illaa', 'fan repair', 'wiring work', 'solar', 'switch board'
        ]
    },
    'plumbing': {
        'canonical': 'Plumbing Repair',
        'category_label': 'Plumbing',
        'category_key': 'plumbing',
        'icon': 'bi bi-wrench-adjustable',
        'badge_bg': '#fff3e0',
        'badge_color': '#e65100',
        'terms': [
            'plumber', 'plumbing', 'pipe', 'pipeline', 'tap', 'leak', 'leakage', 'drain',
            'drainage', 'sink', 'toilet', 'commode', 'flush', 'basin', 'water tank',
            'tank cleaning', 'motor pump', 'borewell', 'well', 'water', 'septic', 'septic tank',
            'plubmer', 'plumer', 'plamber', 'plumbr', 'plumming', 'pumb', 'vellam',
            'choorcha', 'choorchi', 'tap leak', 'pipe block', 'water leak'
        ]
    },
    'cleaning': {
        'canonical': 'Home Cleaning',
        'category_label': 'Home Cleaning',
        'category_key': 'cleaning',
        'icon': 'bi bi-stars',
        'badge_bg': '#e8f5e9',
        'badge_color': '#2e7d32',
        'terms': [
            'cleaning', 'cleaner', 'deep clean', 'house clean', 'home cleaning', 'kitchen clean',
            'bathroom clean', 'chimney', 'sofa clean', 'carpet clean', 'water tank cleaning',
            'sump', 'sanitization', 'shampoo', 'maid', 'sweep', 'mop', 'chlorination',
            'cleanng', 'clening', 'cleanning', 'cleen', 'cleanin', 'veedu clean', 'koodu clean',
            'house washing', 'sofa wash', 'floor cleaning'
        ]
    },
    'ac_repair': {
        'canonical': 'AC Repair & Service',
        'category_label': 'AC Repair',
        'category_key': 'ac_repair',
        'icon': 'bi bi-snow',
        'badge_bg': '#e0f2f1',
        'badge_color': '#00796b',
        'terms': [
            'ac', 'ac repair', 'air conditioner', 'cooling', 'ac service', 'ac install',
            'split ac', 'window ac', 'ac gas', 'compressor', 'cooling coil', 'inverter ac',
            'ac repir', 'ac reapir', 'ac servic', 'ac servise', 'air condition', 'aircondition',
            'ac fix', 'cool', 'chill', 'ac gas filling', 'ac cooling illa', 'ac leak'
        ]
    },
    'carpentry': {
        'canonical': 'Carpentry & Woodwork',
        'category_label': 'Carpentry',
        'category_key': 'carpentry',
        'icon': 'bi bi-hammer',
        'badge_bg': '#fff8e1',
        'badge_color': '#f57f17',
        'terms': [
            'carpenter', 'carpentry', 'wood', 'furniture', 'table', 'chair', 'door', 'window',
            'lock', 'cupboard', 'wardrobe', 'bed', 'shelf', 'hinges', 'plywood', 'wood work',
            'carpentr', 'carpentar', 'carpender', 'carpentary', 'woodwork', 'thadi', 'kattil',
            'door lock', 'door repair', 'window repair', 'wood polish'
        ]
    },
    'painting': {
        'canonical': 'House & Wall Painting',
        'category_label': 'Painting',
        'category_key': 'painting',
        'icon': 'bi bi-paint-bucket',
        'badge_bg': '#fbe9e7',
        'badge_color': '#d84315',
        'terms': [
            'painter', 'painting', 'paint', 'wall paint', 'whitewash', 'distemper',
            'texture', 'waterproofing', 'exterior paint', 'interior paint', 'primer',
            'paintng', 'paintr', 'penting', 'panting', 'niram', 'color', 'colour',
            'wall polish', 'putty', 'wall waterproofing'
        ]
    },
    'appliance': {
        'canonical': 'Appliance Repair',
        'category_label': 'Appliance Repair',
        'category_key': 'appliance',
        'icon': 'bi bi-cpu',
        'badge_bg': '#e8eaf6',
        'badge_color': '#283593',
        'terms': [
            'appliance', 'appliance repair', 'washing machine', 'refrigerator', 'fridge',
            'tv', 'television', 'microwave', 'oven', 'mixer', 'grinder', 'induction',
            'geyser', 'water heater', 'fan repair',
            'apliance', 'washin machin', 'refrigrator', 'refridgerator', 'refregerator',
            'micro wave', 'mixie', 'grinder repair'
        ]
    },
    'gardening': {
        'canonical': 'Gardening & Landscaping',
        'category_label': 'Gardening',
        'category_key': 'gardening',
        'icon': 'bi bi-flower1',
        'badge_bg': '#f1f8e9',
        'badge_color': '#33691e',
        'terms': [
            'gardening', 'gardener', 'lawn', 'grass', 'grass cutting', 'tree', 'tree cutting',
            'coconut', 'coconut tree climber', 'plant', 'bush', 'landscape', 'landscaping',
            'gardning', 'gradening', 'graden', 'thottam', 'pullu', 'maram', 'maram vettal',
            'thellu vett', 'grass trimmer'
        ]
    },
    'pest_control': {
        'canonical': 'Pest Control',
        'category_label': 'Pest Control',
        'category_key': 'pest_control',
        'icon': 'bi bi-bug-fill',
        'badge_bg': '#efebe9',
        'badge_color': '#4e342e',
        'terms': [
            'pest', 'pest control', 'termite', 'cockroach', 'ant', 'mosquito', 'bed bug',
            'rodent', 'rat', 'pestcontrol', 'cheeda', 'paatta', 'karapan', 'chithal',
            'termite control', 'bedbug control'
        ]
    }
}

POPULAR_SERVICES_LIST = [
    {
        'name': 'Electrician',
        'category': 'electrical',
        'category_label': 'Electrical',
        'icon': 'bi bi-lightning-charge-fill',
        'query': 'Electrician'
    },
    {
        'name': 'Plumbing Repair',
        'category': 'plumbing',
        'category_label': 'Plumbing',
        'icon': 'bi bi-wrench-adjustable',
        'query': 'Plumbing'
    },
    {
        'name': 'AC Repair & Service',
        'category': 'ac_repair',
        'category_label': 'AC Repair',
        'icon': 'bi bi-snow',
        'query': 'AC Repair'
    },
    {
        'name': 'Home Cleaning',
        'category': 'cleaning',
        'category_label': 'Cleaning',
        'icon': 'bi bi-stars',
        'query': 'Cleaning'
    },
    {
        'name': 'Carpentry & Woodwork',
        'category': 'carpentry',
        'category_label': 'Carpentry',
        'icon': 'bi bi-hammer',
        'query': 'Carpentry'
    },
    {
        'name': 'Painting',
        'category': 'painting',
        'category_label': 'Painting',
        'icon': 'bi bi-paint-bucket',
        'query': 'Painting'
    },
    {
        'name': 'Appliance Repair',
        'category': 'appliance',
        'category_label': 'Appliance',
        'icon': 'bi bi-cpu',
        'query': 'Appliance'
    },
    {
        'name': 'Gardening & Lawn',
        'category': 'gardening',
        'category_label': 'Gardening',
        'icon': 'bi bi-flower1',
        'query': 'Gardening'
    }
]


def normalize_search_text(text):
    """Normalize text by converting to lowercase and stripping punctuation."""
    if not text:
        return ''
    text = re.sub(r'[^\w\s]', ' ', str(text).lower())
    return ' '.join(text.split())


def calculate_token_similarity(query_norm, target_norm):
    """
    Computes a match score between 0.0 and 1.0 using token containment,
    prefix matching, and difflib SequenceMatcher for typo resilience.
    """
    if not query_norm or not target_norm:
        return 0.0

    if query_norm == target_norm:
        return 1.0

    # Substring containment
    if len(query_norm) >= 3 and query_norm in target_norm:
        return 0.95

    q_words = query_norm.split()
    t_words = target_norm.split()

    # Exact word match
    matching_words = sum(1 for qw in q_words if qw in t_words)
    if matching_words > 0 and len(q_words) > 0:
        return 0.85 + (0.10 * (matching_words / len(q_words)))

    # Prefix match (e.g. "electr" -> "electrician")
    for qw in q_words:
        if len(qw) >= 4:
            for tw in t_words:
                if tw.startswith(qw):
                    return 0.88

    # Full phrase sequence ratio (e.g. "ac repir" vs "ac repair")
    full_ratio = difflib.SequenceMatcher(None, query_norm, target_norm).ratio()
    if full_ratio >= 0.72:
        return full_ratio

    # Word-by-word fuzzy comparison for single typos
    best_word_ratio = 0.0
    for qw in q_words:
        if len(qw) < 3:
            continue
        for tw in t_words:
            if len(tw) < 3:
                continue
            ratio = difflib.SequenceMatcher(None, qw, tw).ratio()
            if ratio > best_word_ratio:
                best_word_ratio = ratio

    if best_word_ratio >= 0.75:
        return best_word_ratio * 0.92

    return 0.0


def deduce_service_category_key(service):
    """Detect or return the category key for a service."""
    cat_key = getattr(service, 'category_key', None)
    if cat_key:
        return cat_key

    name_lower = (getattr(service, 'name', '') or '').lower()
    for key, data in CATEGORY_ALIASES.items():
        if any(term in name_lower for term in data['terms'][:5]):
            return key
    return 'more'


def score_service_against_query(service, query_norm):
    """
    Evaluates a single service against the query.
    Checks name, description, category, and alias terms for its category.
    Returns (score, best_matched_canonical).
    """
    if not query_norm:
        return 0.0, None

    svc_name_norm = normalize_search_text(getattr(service, 'name', ''))
    svc_desc_norm = normalize_search_text(getattr(service, 'description', ''))
    svc_cat_label = normalize_search_text(getattr(service, 'category_label', ''))
    svc_cat_key = deduce_service_category_key(service)

    best_score = 0.0
    best_canonical = getattr(service, 'name', '')

    # 1. Match against service name
    name_score = calculate_token_similarity(query_norm, svc_name_norm)
    if name_score > best_score:
        best_score = name_score
        best_canonical = getattr(service, 'name', '')

    # 2. Match against category label
    if svc_cat_label:
        cat_score = calculate_token_similarity(query_norm, svc_cat_label)
        if cat_score * 0.95 > best_score:
            best_score = cat_score * 0.95
            best_canonical = getattr(service, 'category_label', getattr(service, 'name', ''))

    # 3. Match against service description
    if svc_desc_norm:
        desc_score = calculate_token_similarity(query_norm, svc_desc_norm)
        if desc_score * 0.85 > best_score:
            best_score = desc_score * 0.85

    # 4. Match against CATEGORY_ALIASES terms for this service's category
    if svc_cat_key in CATEGORY_ALIASES:
        cat_data = CATEGORY_ALIASES[svc_cat_key]
        for term in cat_data['terms']:
            term_norm = normalize_search_text(term)
            alias_score = calculate_token_similarity(query_norm, term_norm)
            if alias_score > best_score:
                best_score = alias_score
                best_canonical = cat_data['canonical']

    return best_score, best_canonical


def search_and_rank_services(services_list, raw_query, min_score=0.48):
    """
    Filters and ranks a list of services based on fuzzy similarity and alias mapping.
    Returns:
        (ranked_services, corrected_query, had_typo)
    """
    query_norm = normalize_search_text(raw_query)
    if not query_norm:
        return list(services_list), None, False

    scored_items = []
    top_canonical_votes = {}

    for s in services_list:
        score, canonical = score_service_against_query(s, query_norm)
        if score >= min_score:
            scored_items.append((score, s, canonical))
            if canonical:
                top_canonical_votes[canonical] = top_canonical_votes.get(canonical, 0) + score

    # Sort descending by match score
    scored_items.sort(key=lambda item: item[0], reverse=True)
    ranked_services = [item[1] for item in scored_items]

    # Check if this was a typo correction
    had_typo = False
    corrected_query = None

    if top_canonical_votes and ranked_services:
        best_canonical = max(top_canonical_votes.items(), key=lambda x: x[1])[0]
        is_exact_match = any(query_norm in normalize_search_text(s.name) for s in ranked_services)
        if not is_exact_match and query_norm != normalize_search_text(best_canonical):
            had_typo = True
            corrected_query = best_canonical

    return ranked_services, corrected_query, had_typo


def get_search_suggestions_payload(services_list, raw_query, district='Kollam', limit=8):
    """
    Generates a structured dictionary for the live autocomplete dropdown.
    """
    query_norm = normalize_search_text(raw_query)

    if not query_norm:
        return {
            'status': 'success',
            'query': '',
            'corrected_query': None,
            'has_typo': False,
            'suggestions': [],
            'popular': POPULAR_SERVICES_LIST
        }

    ranked, corrected_query, had_typo = search_and_rank_services(services_list, raw_query, min_score=0.45)

    suggestions = []
    for s in ranked[:limit]:
        suggestions.append({
            'id': s.id,
            'name': s.name,
            'category_key': getattr(s, 'category_key', 'more'),
            'category_label': getattr(s, 'category_label', 'Service'),
            'icon_class': getattr(s, 'icon_class', 'bi bi-tools'),
            'badge_bg': getattr(s, 'badge_bg', '#f1f5f9'),
            'badge_color': getattr(s, 'badge_color', '#087f5b'),
            'worker_count': getattr(s, 'real_worker_count', 0),
            'pro_count_display': getattr(s, 'pro_count_display', ''),
            'url': f"/services/{s.id}/workers/?district={district}" if district else f"/services/{s.id}/workers/",
            'service_page_url': f"/service_page/?q={s.name}&district={district}" if district else f"/service_page/?q={s.name}"
        })

    return {
        'status': 'success',
        'query': raw_query,
        'corrected_query': corrected_query,
        'has_typo': had_typo,
        'suggestions': suggestions,
        'popular': POPULAR_SERVICES_LIST if len(suggestions) < 3 else []
    }
