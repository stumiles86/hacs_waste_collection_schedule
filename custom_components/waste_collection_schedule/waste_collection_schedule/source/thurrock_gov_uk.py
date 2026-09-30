import time
from datetime import date, datetime, timedelta

import requests
from waste_collection_schedule import Collection, Icons  # type: ignore[attr-defined]
from waste_collection_schedule.exceptions import (
    SourceArgumentExceptionMultiple,
    SourceArgumentNotFoundWithSuggestions,
    SourceArgumentRequired,
)

TITLE = "Thurrock"
DESCRIPTION = "Source for Thurrock Council waste collections (services.thurrock.gov.uk)."
URL = "https://www.thurrock.gov.uk/"
TEST_CASES = {
    "Camden Close": {
        "postcode": "RM16 4HT",
        "house": "1",
    },
    "Abberton Way West Thurrock": {
        "postcode": "RM20 3BB",
        "house": "1",
    },
}

ICON_MAP = {
    "Refuse": Icons.GENERAL_WASTE,
    "Recycling": Icons.RECYCLING,
    "Food": Icons.BIO_KITCHEN,
    "Garden": Icons.GARDEN,
}

SERVICE_MAP = {
    "Domestic Empty Refuse 180": "Refuse",
    "Domestic Empty Refuse 240": "Refuse",
    "Domestic Empty Recycling 240": "Recycling",
    "Domestic Empty Recycling 180": "Recycling",
    "Domestic Empty 23L Food Caddy": "Food",
    "Domestic Empty Food Caddy": "Food",
    "Domestic Empty Garden 240": "Garden",
    "Domestic Empty Garden": "Garden",
}

BASE = "https://services.thurrock.gov.uk"
SESSION_URL = f"{BASE}/en/service/Waste_and_recycling_bin_collection_schedule"
AUTH_URL = (
    f"{BASE}/authapi/isauthenticated"
    "?uri=https%253A%252F%252Fservices.thurrock.gov.uk"
    "%252Fen%252Fservice%252FWaste_and_recycling_bin_collection_schedule"
    "&hostname=services.thurrock.gov.uk&withCredentials=true"
)

LOOKUP_AUTH = "5f68c66dcc8f6"
LOOKUP_ADDRESS = "67f50a0b2b240"
LOOKUP_SCHEDULE = "6836e0d463cd2"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
}

SCHEDULE_DAYS = 56


class Source:
    def __init__(self, postcode, house):
        if not postcode:
            raise SourceArgumentRequired("postcode", "a postcode is required")
        if house is None or str(house).strip() == "":
            raise SourceArgumentRequired("house", "a house number or name is required")

        self._postcode = str(postcode).strip()
        self._house = str(house).strip()

    def _run_lookup(self, session, sid, lookup_id, form_values, extra_top=None):
        now = int(time.time() * 1000)
        url = (
            f"{BASE}/apibroker/runLookup?id={lookup_id}"
            f"&repeat_against=&noRetry=false&getOnlyTokens=undefined"
            f"&log_id=&app_name=AF-Renderer::Self&_={now}&sid={sid}"
        )
        body = {"formValues": form_values}
        if extra_top:
            body.update(extra_top)
        r = session.post(url, json=body, headers=HEADERS, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            raise Exception(
                f"Thurrock lookup {lookup_id} failed: {data.get('error')}"
            )
        return data.get("integration", {}).get("transformed", {}).get("rows_data") or {}

    def _get_session(self):
        session = requests.Session()
        session.headers.update({"User-Agent": HEADERS["User-Agent"]})
        session.get(SESSION_URL, timeout=30).raise_for_status()
        auth = session.get(AUTH_URL, timeout=30).json()
        sid = auth["auth-session"]
        rows = self._run_lookup(session, sid, LOOKUP_AUTH, {"Section 1": {}})
        token = next(iter(rows.values()))["AuthenticateResponse"]
        return session, sid, token

    def _resolve_uprn(self, session, sid, token):
        rows = self._run_lookup(
            session,
            sid,
            LOOKUP_ADDRESS,
            {
                "Section 1": {
                    "AuthenticateResponse": {
                        "name": "AuthenticateResponse",
                        "value": token,
                    },
                    "postcode_search": {
                        "name": "postcode_search",
                        "value": self._postcode,
                    },
                }
            },
        )
        if not rows:
            raise SourceArgumentExceptionMultiple(
                ["postcode", "house"],
                f"No addresses found for postcode '{self._postcode}'",
            )

        candidates = list(rows.values())
        want = self._house.casefold()
        matched = []
        for row in candidates:
            house = (row.get("house") or "").strip().casefold()
            display = (row.get("display") or "").casefold()
            flat_house = (row.get("flatHouse") or "").strip().casefold()
            first_part = display.split(",")[0].strip()

            if (
                house == want
                or flat_house == want
                or first_part == want
                or first_part.startswith(want + " ")
                or display.startswith(want + " ")
            ):
                matched.append(row)

        if len(matched) == 1:
            return str(matched[0]["uprn"])

        suggestions = [c.get("display") or c.get("uprn") for c in candidates[:20]]
        raise SourceArgumentNotFoundWithSuggestions(
            "house",
            self._house,
            suggestions,
        )

    def _friendly_type(self, service_name):
        if service_name in SERVICE_MAP:
            return SERVICE_MAP[service_name]
        name = service_name.casefold()
        if "fly tip" in name or "missed" in name or "bulky" in name:
            return None
        if "food" in name:
            return "Food"
        if "recycl" in name:
            return "Recycling"
        if "garden" in name or "green waste" in name:
            return "Garden"
        if "refuse" in name or "residual" in name or "general" in name:
            return "Refuse"
        return service_name

    def fetch(self):
        session, sid, token = self._get_session()
        uprn = self._resolve_uprn(session, sid, token)

        today = date.today()
        min_d = today.isoformat()
        max_d = (today + timedelta(days=SCHEDULE_DAYS)).isoformat()

        rows = self._run_lookup(
            session,
            sid,
            LOOKUP_SCHEDULE,
            {
                "Section 1": {
                    "AuthenticateResponse": {
                        "name": "AuthenticateResponse",
                        "value": token,
                    },
                    "LookupUPRN": {"name": "LookupUPRN", "value": uprn},
                    "txtAddress": {"name": "txtAddress", "value": uprn},
                    "MinLimitDate": {"name": "MinLimitDate", "value": min_d},
                    "MaxLimitDate": {"name": "MaxLimitDate", "value": max_d},
                }
            },
            extra_top={
                "stopOnFailure": True,
                "usePHPIntegrations": True,
                "stage_id": "AF-Stage-78254f6b-c18e-4827-89a3-a338921fc776",
                "stage_name": "Initial request",
                "formId": "AF-Form-1d05ee4f-0bd0-4161-bfa0-c3ad46e94196",
                "isPublished": True,
                "formName": "Waste and recycling - collection schedules",
                "processId": "AF-Process-539e654b-482b-4bd2-991b-0015bbbf2c70",
            },
        )

        entries = []
        for row in rows.values():
            name = (row.get("Name") or "").strip()
            start = (row.get("ScheduledStart") or "").strip()
            if not name or not start:
                continue
            friendly = self._friendly_type(name)
            if friendly is None:
                continue
            try:
                col_date = datetime.fromisoformat(start).date()
            except ValueError:
                col_date = datetime.strptime(start[:10], "%Y-%m-%d").date()
            entries.append(
                Collection(
                    date=col_date,
                    t=friendly,
                    icon=ICON_MAP.get(friendly),
                )
            )

        if not entries:
            raise Exception(
                f"No collection dates returned for {self._house}, {self._postcode}. "
                "The council form only supports houses and bag-only properties."
            )

        return entries
