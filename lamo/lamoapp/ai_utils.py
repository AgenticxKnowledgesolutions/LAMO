import json
import urllib.request
from django.conf import settings
from django.db.models import Avg, Q
from .models import Service, WorkerProfile


def _call_gemini_api(prompt_text):
    """
    Call Google Gemini API using REST.
    Returns response text or None if the request fails.
    """

    # Get API key ONLY from Django settings
    api_key = getattr(settings, 'GOOGLE_API_KEY', '')

    if not api_key:
        print("Gemini API key is missing.")
        return None

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-2.0-flash:generateContent"
        f"?key={api_key}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt_text
                    }
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
                "Content-Type": "application/json"
            },
            method="POST"
        )

        with urllib.request.urlopen(
            req,
            timeout=15
        ) as response:

            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)

            candidates = res_json.get(
                "candidates",
                []
            )

            if candidates:

                parts = (
                    candidates[0]
                    .get("content", {})
                    .get("parts", [])
                )

                if parts:
                    return (
                        parts[0]
                        .get("text", "")
                        .strip()
                    )

    except Exception as e:
        print("Gemini API Error:", e)

    return None


# ==========================================================
# GENERAL AI CHAT
# ==========================================================

def chat_with_gemini(user_message):

    prompt = f"""
You are the friendly AI assistant of LAMO,
a home service marketplace.

Talk naturally like a helpful human assistant.

User message:
"{user_message}"

Rules:

1. If the user says hello, hi, hey, etc.,
   greet them naturally.

2. If the user asks "how are you",
   respond naturally and positively.

3. If the user asks what you can do,
   explain that you can help them find home
   service providers.

4. If the user asks about LAMO,
   explain that LAMO helps customers find
   home service professionals.

5. Keep replies short, friendly and natural.

6. Do not invent worker names, prices,
   ratings, bookings or other information.

7. Do not pretend that a booking has been made.

8. If the user wants to find a service provider,
   tell them that you can help them find the
   appropriate service.

Return only the assistant's natural reply.
"""

    return _call_gemini_api(prompt)


# ==========================================================
# MATCH SERVICE AND WORKERS
# ==========================================================

def match_service_and_workers(user_query, district=None):

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
        "You are an AI assistant for a home "
        "service marketplace called LAMO.\n\n"
        "Available services:\n"
    )

    for s in services:

        prompt += (
            f"- {s.name}: "
            f"{s.description}\n"
        )

    prompt += (
        f"\nCustomer request: '{user_query}'\n\n"
        "Identify the single best matching service "
        "from the available services.\n"
        "Return ONLY the exact service name.\n"
        "Do not add punctuation or explanation."
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
    # DATABASE FALLBACK
    # ------------------------------------------------------

    if not matched_service:

        matched_service = services.filter(
            Q(name__icontains=user_query)
            |
            Q(description__icontains=user_query)
        ).first()

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
        work_status='AVAILABLE'
    ).select_related(
        'user',
        'service'
    )

    if district:

        workers_qs = workers_qs.filter(
            district__iexact=district
        )

    workers_qs = (
        workers_qs
        .annotate(
            avg_rating=Avg(
                'reviews_received__rating'
            )
        )
        .order_by(
            '-avg_rating',
            '-experience_years'
        )[:5]
    )

    return {
        'service': matched_service,
        'workers': list(workers_qs),
        'query': user_query
    }


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