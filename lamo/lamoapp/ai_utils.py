import json
import logging
import math
import re
import urllib.request
import urllib.parse
import urllib.error
from functools import lru_cache
from django.conf import settings
from django.db.models import Avg, Q, Count
from .models import Booking, Review, Service, WorkerProfile

logger = logging.getLogger(__name__)

DISTRICT_COORDINATES = {
    'Thiruvananthapuram': (8.5241, 76.9366),
    'Kollam': (8.8932, 76.6141),
    'Pathanamthitta': (9.2648, 76.7874),
    'Alappuzha': (9.4981, 76.3388),
    'Kottayam': (9.5916, 76.5224),
    'Idukki': (9.9187, 77.1025),
    'Ernakulam': (9.9312, 76.2673),
    'Thrissur': (10.5276, 76.2144),
    'Palakkad': (10.7867, 76.6548),
    'Malappuram': (11.0732, 76.1412),
    'Kozhikode': (11.2588, 75.7804),
    'Wayanad': (11.7153, 76.2525),
    'Kannur': (11.8745, 75.3704),
    'Kasaragod': (12.4996, 75.0096),
}


def haversine_km(lat1, lon1, lat2, lon2):
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c


def distance_score(distance_km):
    """Map a distance in kilometres onto a 0-100 proximity score.

    Single source of truth for the banding used when ranking workers by how
    close they are to the customer. Keeping it here means the ordering in
    ``match_service_and_workers`` and the score reported by
    ``build_worker_recommendations`` can never drift apart.

    Returns 0 when ``distance_km`` is None (coordinates unavailable).
    """
    if distance_km is None:
        return 0
    if distance_km <= 2:
        return 95
    if distance_km <= 5:
        return 82
    if distance_km <= 10:
        return 68
    if distance_km <= 20:
        return 48
    return max(0, 100 - (distance_km * 3))


def _call_gemini_api(prompt_text):
    """
    Call Google Gemini API using REST.
    Returns response text or None if the request fails.
    The AI feature is optional; if the external API is unavailable,
    the app should continue using local database fallback logic.
    """

    api_key = getattr(settings, 'GOOGLE_API_KEY', '').strip()

    if not api_key or len(api_key) < 20:
        return None

    # The key travels in the x-goog-api-key HEADER, never in the query string.
    # Gemini supports both, but a query-string key ends up in the request line
    # and therefore in every proxy / CDN / load-balancer access log between us
    # and Google, which is a far wider blast radius than the app's own logs.
    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-2.0-flash:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt_text}
                ]
            }
        ]
    }

    try:
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=6) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)

            candidates = res_json.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()

        return None

    except urllib.error.HTTPError as exc:
        # Operational signal only: the status code. The URL is deliberately not
        # logged (it is credential-free now, but the habit is what protects the
        # key), and the response body can echo request details back.
        logger.warning(
            'gemini_http_error status=%s model=gemini-2.0-flash',
            exc.code,
        )
        return None
    except Exception as exc:
        # Class name only. exc.args can contain the key or the request body.
        logger.warning(
            'gemini_request_failed exc_type=%s',
            type(exc).__name__,
        )
        return None


# ==========================================================
# GENERAL AI CHAT
# ==========================================================

def detect_user_language(text):
    """
    Detects whether the user is typing in Malayalam script, Manglish (Malayalam in Latin script),
    or English. Returns 'malayalam', 'manglish', or 'english'.
    """
    if not text:
        return 'english'

    # Check for Malayalam Unicode range (U+0D00 to U+0D7F)
    malayalam_chars = sum(1 for c in text if '\u0D00' <= c <= '\u0D7F')
    if malayalam_chars >= 2 or (len(text.strip()) > 0 and malayalam_chars / len(text.strip()) > 0.2):
        return 'malayalam'

    lower = text.lower()
    manglish_markers = [
        'venam', 'vnm', 'aavashyam', 'avashyam', 'undo', 'undoo', 'cheyyamo',
        'cheyyanam', 'cheyyan', 'enthokke', 'enthoke', 'entha', 'endha', 'vishesham',
        'visesham', 'sugam', 'sugamano', 'sukham', 'sukhamano', 'evide', 'engane',
        'ippol', 'ippo', 'namaskaram', 'namaskaarom', 'sahayam', 'sahayikamo',
        'kitti', 'kittumo', 'varumo', 'varan', 'aanu', 'anu', 'aano', 'ano',
        'enik', 'enikk', 'enikku', 'njan', 'njn', 'oru', 'aarokke', 'aarkk',
        'pattum', 'nannayi', 'nalla', 'athra', 'ithra', 'ningal', 'ningalkk',
        'vannu', 'pokumo', 'poyikko'
    ]

    words = re.findall(r'[a-z]+', lower)
    if any(w in manglish_markers for w in words):
        return 'manglish'

    return 'english'


# Common Malayalam and Manglish service synonym maps
SERVICE_SYNONYM_MAP = {
    'electrician': [
        'electrician', 'electrical', 'wiring', 'fuse', 'light', 'fan', 'switch', 'inverter',
        'current', 'plug', 'current poyi', 'current illa', 'short circuit',
        # Manglish
        'electrisian', 'electrishen', 'ilectrician', 'ilectrishen', 'currentukaran',
        'currentkaran', 'currentkaaran',
        # Malayalam
        'ഇലക്ട്രീഷ്യൻ', 'ഇലക്ട്രിഷ്യൻ', 'ഇലക്ട്രിക്കൽ', 'വയറിങ്', 'വയറിംഗ്', 'കറന്റ്', 'ലൈറ്റ്', 'ഫാൻ', 'സ്വിച്ച്'
    ],
    'plumber': [
        'plumber', 'plumbing', 'pipe', 'leak', 'tap', 'water leak', 'flush', 'drain', 'basin',
        'pipe potti', 'vellam leak', 'vellam chorum',
        # Manglish
        'plamber', 'plambur', 'plumberkaran', 'vellakkaran',
        # Malayalam
        'പ്ലംബർ', 'പ്ലമ്പർ', 'പൈപ്പ്', 'ടാപ്പ്', 'വെള്ളം ചോർച്ച', 'പൈപ്പ് പൊട്ടി', 'പ്ലംബിങ്', 'പ്ലംബിംഗ്'
    ],
    'carpenter': [
        'carpenter', 'carpentry', 'wood', 'furniture', 'door', 'window', 'table', 'chair',
        # Manglish
        'carpanter', 'karpenter', 'marapani', 'aasaari', 'asari', 'wood work',
        # Malayalam
        'ആശാരി', 'തച്ചൻ', 'മരപ്പണി', 'ഫർണിച്ചർ', 'വാതിൽ', 'ജനൽ', 'കാർപെന്റർ'
    ],
    'cleaner': [
        'cleaning', 'cleaner', 'house cleaning', 'wash', 'sweeping', 'mopping', 'maid',
        # Manglish
        'cleaners', 'veedu clean', 'veed clean', 'cleaningukari', 'kazhukan',
        # Malayalam — 'ക്ലീന' is the shared stem covering both ക്ലീനർ (U+0D7C)
        # and declined forms like ക്ലീനറെ (U+0D31 + vowel sign)
        'ക്ലീന', 'ക്ലീനർ', 'ക്ലീനിങ്', 'വീട് വൃത്തിയാക്കൽ', 'വൃത്തിയാക്കാൻ', 'തുടക്കാൻ', 'അടിക്കാൻ'
    ],
    'painter': [
        'painter', 'painting', 'wall paint', 'whitewash',
        # Manglish
        'peyntar', 'peynting', 'chumar paint', 'veed paint',
        # Malayalam
        'പെയിന്റർ', 'പെയിന്റിങ്', 'പെയിന്റിംഗ്', 'നിറം പൂശൽ', 'ചായം പൂശൽ'
    ],
    'mechanic': [
        'mechanic', 'repair', 'ac repair', 'fridge repair', 'washing machine', 'appliance',
        # Manglish
        'mekkanik', 'reppeyr', 'vandi repair',
        # Malayalam
        'മെക്കാനിക്', 'റിപ്പയർ', 'യന്ത്രപ്പണി'
    ],
}


def chat_with_gemini(user_message):
    lang = detect_user_language(user_message)

    prompt = f"""
You are the friendly, intelligent AI assistant of LAMO, a home service marketplace in Kerala.
You must communicate fluently in English, Malayalam (മലയാളം), and Manglish (Malayalam written using English letters).

User message:
"{user_message}"

Detected language / style: {lang}

Rules:
1. Respond in the EXACT SAME LANGUAGE and style used by the user:
   - If the user writes in Malayalam script (മലയാളം), reply in natural, polite Malayalam.
   - If the user writes in Manglish (Malayalam in English letters like "sugamano", "enik electrician venam"), reply in natural Manglish!
   - If the user writes in English, reply in English.
2. If the user greets (hello, hi, namaskaram, sugamano, etc.):
   - In Malayalam: "നമസ്കാരം! 👋 ഞാൻ LAMO അസിസ്റ്റന്റ് ആണ്. ഇന്ന് എന്ത് സഹായമാണ് വേണ്ടത്?"
   - In Manglish: "Namaskaram! 👋 Njan LAMO assistant aanu. Enthu sahayamanu vendathu?"
   - In English: "Hello! 👋 I'm the LAMO assistant. How can I help you today?"
3. If the user asks for a service or service workers:
   - Politely tell them you can help find available service professionals on LAMO.
4. If the user asks a question about services or details:
   - Ask clarifying follow-up questions if needed (e.g., location, type of issue) in their language.
5. Keep replies short, polite, helpful, and natural (1-3 sentences max).
6. DO NOT invent fake worker names, phone numbers, prices, or ratings.
7. DO NOT pretend a booking is confirmed.

Return only the assistant's natural reply.
"""

    return _call_gemini_api(prompt)


# ==========================================================
# MATCH SERVICE AND WORKERS
# ==========================================================

def match_service_and_workers(user_query, district=None, customer_lat=None, customer_lng=None):

    services = Service.objects.filter(
        is_active=True
    )

    if not services.exists():
        return {
            'service': None,
            'workers': [],
            'query': user_query
        }

    matched_service = None

    prompt = (
        "You are an AI assistant for a Kerala home service marketplace called LAMO.\n"
        "Customers may express their requests in English, Malayalam script (മലയാളം), or Manglish (e.g., 'Enik oru electrician venam', 'Oru plumber venam', 'വൈദ്യുതി തകരാർ പരിഹരിക്കാൻ ഒരാളെ വേണം').\n\n"
        "Available services:\n"
    )

    for s in services:
        prompt += (
            f"- {s.name}: "
            f"{s.description}\n"
        )

    prompt += (
        f"\nCustomer request: '{user_query}'\n\n"
        "Understand the request in English, Malayalam, or Manglish and identify the single best matching service name from the available services listed above.\n"
        "Return ONLY the exact service name as it appears in the list.\n"
        "Do not add punctuation, quotes, or explanation."
    )

    gemini_result = _call_gemini_api(prompt)

    if gemini_result:

        clean_res = (
            gemini_result
            .strip()
            .strip("'\"`.")
        )

        matched_service = (
            services
            .filter(
                name__iexact=clean_res
            )
            .first()
        )

    # ------------------------------------------------------
    # DATABASE & KEYWORD FALLBACK (Handles Manglish, Malayalam, English)
    # ------------------------------------------------------

    if not matched_service:
        # Check against multilingual synonym dictionary
        lower_query = user_query.lower()
        matched_category = None

        for category, keywords in SERVICE_SYNONYM_MAP.items():
            for kw in keywords:
                # Match full word or substring for Malayalam/Manglish tokens
                if re.search(r'\b' + re.escape(kw) + r'\b', lower_query) or (len(kw) >= 4 and kw in lower_query):
                    matched_category = category
                    break
            if matched_category:
                break

        if matched_category:
            for s in services:
                s_name_lower = s.name.lower()
                if matched_category in s_name_lower or s_name_lower in matched_category:
                    matched_service = s
                    break
                # Special cases: electrical / electrician
                if matched_category == 'electrician' and ('electric' in s_name_lower or 'wiring' in s_name_lower):
                    matched_service = s
                    break
                if matched_category == 'plumber' and ('plumb' in s_name_lower or 'pipe' in s_name_lower):
                    matched_service = s
                    break
                if matched_category == 'carpenter' and ('carpent' in s_name_lower or 'wood' in s_name_lower):
                    matched_service = s
                    break
                if matched_category == 'cleaner' and ('clean' in s_name_lower):
                    matched_service = s
                    break
                if matched_category == 'painter' and ('paint' in s_name_lower):
                    matched_service = s
                    break

        if not matched_service:
            # Fallback to English/Latin and Malayalam stem matching
            STOP_WORDS = {
                'i', 'me', 'my', 'we', 'us', 'our', 'you', 'your',
                'need', 'want', 'looking', 'for', 'a', 'an', 'the',
                'please', 'can', 'could', 'would', 'like', 'to', 'get',
                'find', 'help', 'someone', 'any', 'is', 'are', 'am',
                'do', 'does', 'did', 'have', 'has', 'had', 'with',
                'and', 'or', 'but', 'in', 'on', 'at', 'of', 'it', 'that',
                'this', 'there', 'here', 'urgent', 'quickly', 'soon', 'now',
                # Manglish stop words
                'enik', 'enikk', 'enikku', 'oru', 'venam', 'vnm', 'aavashyam',
                'avashyam', 'sahayam', 'undo', 'undoo', 'cheyyamo', 'kittumo',
                'aano', 'ano', 'aanu', 'anu', 'evide', 'njan', 'njn',
                # Malayalam stop words
                'എനിക്ക്', 'ഒരു', 'വേണം', 'സഹായം', 'ഉണ്ടോ', 'ആണ്'
            }

            tokens = [
                t for t in re.split(r'[^a-zA-Z0-9\u0D00-\u0D7F]+', user_query.lower())
                if len(t) > 2 and t not in STOP_WORDS
            ]

            stems = [t[:5] for t in tokens]

            best_service = None
            best_score = 0

            for candidate in services:
                name_text = (candidate.name or '').lower()
                full_text = f"{name_text} {(candidate.description or '').lower()}"

                score = 0
                for stem in stems:
                    if stem in name_text:
                        score += 2
                    elif stem in full_text:
                        score += 1

                if score > best_score:
                    best_score = score
                    best_service = candidate

            if best_score >= 2:
                matched_service = best_service

    # ------------------------------------------------------
    # DO NOT RANDOMLY SELECT FIRST SERVICE
    # ------------------------------------------------------

    # Important:
    # If nothing matches, return None instead of
    # incorrectly selecting the first service.

    if not matched_service:

        return {
            'service': None,
            'workers': [],
            'query': user_query
        }

    # ------------------------------------------------------
    # AVAILABLE WORKERS
    # ------------------------------------------------------

    workers_qs = WorkerProfile.objects.filter(
        service=matched_service,
        is_verified=True,
        work_status__in=['AVAILABLE', 'ON_DUTY']
    ).select_related(
        'user',
        'service'
    )

    if district:

        workers_qs = workers_qs.filter(
            district__iexact=district
        )

    workers = list(
        workers_qs.annotate(
            avg_rating=Avg(
                'reviews_received__rating'
            )
        )
    )

    # ------------------------------------------------------
    # DISTANCE-AWARE RANKING
    # ------------------------------------------------------

    customer_lat_float = _safe_float(customer_lat)
    customer_lng_float = _safe_float(customer_lng)

    def worker_distance_score(worker):
        worker_lat = _safe_float(getattr(worker, 'latitude', None))
        worker_lng = _safe_float(getattr(worker, 'longitude', None))

        if worker_lat is None or worker_lng is None:
            worker_place = (worker.address or '').strip()
            if worker_place:
                worker_lat, worker_lng = geocode_place(worker_place, district=worker.district)

        if (
            customer_lat_float is not None
            and customer_lng_float is not None
            and worker_lat is not None
            and worker_lng is not None
        ):
            distance_km = haversine_km(
                customer_lat_float,
                customer_lng_float,
                worker_lat,
                worker_lng,
            )
            return distance_score(distance_km), distance_km

        return 0, None

    def sort_key(worker):
        distance_score, distance_km = worker_distance_score(worker)
        avg_rating = getattr(worker, 'avg_rating', 0) or 0
        experience = getattr(worker, 'experience_years', 0) or 0
        return (
            -distance_score,
            -(avg_rating / 5) * 100,
            -experience,
        )

    workers.sort(key=sort_key)

    return {
        'service': matched_service,
        'workers': workers[:5],
        'query': user_query
    }


@lru_cache(maxsize=256)
def geocode_place(place_name, district=None, state='Kerala'):
    place_value = (place_name or '').strip()
    if not place_value:
        return None, None

    query = place_value
    if district:
        query = f"{place_value}, {district}, {state}"
    elif place_value.lower() not in {'kerala', 'india'}:
        query = f"{place_value}, Kerala"

    encoded = urllib.parse.quote(query)
    url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q={encoded}"

    try:
        request = urllib.request.Request(url, headers={'User-Agent': settings.GEOCODING_USER_AGENT})
        with urllib.request.urlopen(request, timeout=4) as response:
            data = json.loads(response.read().decode('utf-8'))
            if not data:
                return None, None
            first = data[0]
            lat = first.get('lat')
            lon = first.get('lon')
            if lat is None or lon is None:
                return None, None
            return float(lat), float(lon)
    except Exception:
        return None, None


def _safe_float(value):
    if value in (None, '', 'None', 'null', 'undefined'):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_worker_recommendations(
    service,
    district=None,
    requirement="",
    customer_lat=None,
    customer_lng=None,
    customer_place=None,
    customer_address=None,
):
    """
    Use actual customer and worker location data whenever available.
    District is kept only as a business/eligibility factor and not treated as a real distance.
    """
    if not service:
        return []

    workers_qs = WorkerProfile.objects.filter(
        service=service,
        is_verified=True,
        work_status__in=['AVAILABLE', 'ON_DUTY']
    ).select_related(
        'user',
        'service'
    ).annotate(
        avg_rating=Avg('reviews_received__rating'),
        completed_jobs=Count(
            'bookings',
            filter=Q(bookings__status='COMPLETED'),
            distinct=True,
        ),
        cancelled_jobs=Count(
            'bookings',
            filter=Q(bookings__status='CANCELLED'),
            distinct=True,
        ),
    )

    if district:
        workers_qs = workers_qs.filter(district__iexact=district)

    workers = list(workers_qs)

    if not workers:
        return []

    requirement_text = (requirement or '').lower().strip()
    customer_lat_float = _safe_float(customer_lat)
    customer_lng_float = _safe_float(customer_lng)

    if customer_lat_float is None or customer_lng_float is None:
        candidate_place = (customer_place or customer_address or '').strip()
        if candidate_place:
            customer_lat_float, customer_lng_float = geocode_place(candidate_place, district=district)

    recommendations = []

    for worker in workers:
        avg_rating = worker.avg_rating or 0
        completed_jobs = worker.completed_jobs or 0
        cancelled_jobs = worker.cancelled_jobs or 0

        worker_place = (worker.address or '').strip()
        worker_lat_float = _safe_float(getattr(worker, 'latitude', None))
        worker_lng_float = _safe_float(getattr(worker, 'longitude', None))

        if worker_lat_float is None or worker_lng_float is None:
            if worker_place:
                worker_lat_float, worker_lng_float = geocode_place(worker_place, district=worker.district)

        distance_km = None
        proximity_score = 0
        distance_label = 'Exact distance unavailable'
        location_hint = worker_place or worker.district or 'Location available'

        if customer_lat_float is not None and customer_lng_float is not None and worker_lat_float is not None and worker_lng_float is not None:
            distance_km = haversine_km(
                customer_lat_float,
                customer_lng_float,
                worker_lat_float,
                worker_lng_float,
            )
            proximity_score = distance_score(distance_km)
            distance_label = f'{distance_km:.1f} km away'

        # District is used as a filtering/business eligibility check only.
        # It must never be treated as a real physical distance or an exact
        # location match without actual geocoded/coordinated addresses.
        # When coordinates are unavailable, the score should remain honest and
        # the UI should show that exact distance is unavailable instead of a
        # fake district-based distance.

        service_score = 100
        availability_score = {
            'AVAILABLE': 100,
            'ON_DUTY': 75,
            'LEAVE': 20,
            'SUSPENDED': 0,
        }.get(worker.work_status, 0)

        verification_score = 100 if worker.is_verified else 0
        rating_score = min(100, (avg_rating / 5) * 100) if avg_rating else 60
        completed_jobs_score = min(100, (completed_jobs / 20) * 100) if completed_jobs else 25
        reliability_score = max(0, 100 - (cancelled_jobs * 15))
        experience_score = min(100, (worker.experience_years / 10) * 100) if worker.experience_years else 50

        requirement_tokens = [
            token.lower() for token in re.split(r'[^a-zA-Z0-9]+', requirement_text) if token
        ]
        requirement_bonus = 0
        if requirement_tokens:
            service_text = f"{service.name} {service.description or ''}".lower()
            requirement_bonus = min(10, sum(1 for token in requirement_tokens if token in service_text) * 3)

        match_score = round(
            (
                service_score * 0.30
                + availability_score * 0.18
                + proximity_score * 0.22
                + rating_score * 0.15
                + verification_score * 0.10
                + experience_score * 0.05
            ) / 100 * 100 + requirement_bonus,
            1,
        )
        match_score = max(0, min(100, match_score))

        reasons = []
        if worker.service_id == service.id:
            reasons.append('Required service match')
        if worker.work_status == 'AVAILABLE':
            reasons.append('Available for booking')
        elif worker.work_status == 'ON_DUTY':
            reasons.append('Currently on duty')
        if distance_km is not None:
            reasons.append(f'{distance_km:.1f} km from your location')
        elif location_hint:
            reasons.append(f'Location available: {location_hint}')
        if worker.is_verified:
            reasons.append('Verified worker')
        if avg_rating:
            reasons.append(f'{avg_rating:.1f}/5 average rating')
        if completed_jobs:
            reasons.append(f'{completed_jobs} completed jobs')
        if not reasons:
            reasons.append('Relevant worker profile match')

        worker_img_url = ''
        if worker.profile_image:
            try:
                worker_img_url = worker.profile_image.url
            except Exception:
                worker_img_url = ''
        elif hasattr(worker.user, 'profile_image') and worker.user.profile_image:
            try:
                worker_img_url = worker.user.profile_image.url
            except Exception:
                worker_img_url = ''

        recommendations.append({
            'worker_id': worker.id,
            'worker_name': worker.user.get_full_name() or worker.user.username,
            'worker_image': worker_img_url,
            'service_name': service.name,
            'district': worker.district,
            'match_score': match_score,
            'distance_km': round(distance_km, 1) if distance_km is not None else None,
            'distance_label': distance_label,
            'location_label': location_hint,
            'availability': worker.work_status,
            'verified': worker.is_verified,
            'avg_rating': round(avg_rating, 1) if avg_rating else None,
            'completed_jobs': completed_jobs,
            'reasons': reasons[:5],
            'reliability_score': round(reliability_score, 1),
            'worker': worker,
        })

    return sorted(recommendations, key=lambda item: item['match_score'], reverse=True)[:5]


# ==========================================================
# COMPLAINT SEVERITY
# ==========================================================

def classify_complaint_severity(
    subject,
    description
):

    prompt = (
        "Classify the urgency/severity of the "
        "following customer complaint into exactly "
        "ONE word: LOW, MEDIUM, or HIGH.\n\n"

        f"Subject: {subject}\n"
        f"Description: {description}\n\n"

        "Rules:\n"
        "- HIGH: Safety issues, fraud, property "
        "damage, harassment, severe unresponsiveness.\n"

        "- MEDIUM: Subpar service, delay, moderate "
        "dispute, incomplete work.\n"

        "- LOW: Minor miscommunication, polite feedback.\n\n"

        "Return ONLY one word: "
        "LOW, MEDIUM, or HIGH."
    )

    res = _call_gemini_api(prompt)

    if res:

        res_upper = res.upper()

        if 'HIGH' in res_upper:
            return 'HIGH'

        if 'LOW' in res_upper:
            return 'LOW'

        if 'MEDIUM' in res_upper:
            return 'MEDIUM'

    return 'MEDIUM'