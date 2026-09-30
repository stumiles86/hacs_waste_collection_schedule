import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from dateutil.rrule import FR, MO, SA, SU, TH, TU, WE, WEEKLY, rrule, weekday
from waste_collection_schedule import Collection, Icons  # type: ignore[attr-defined]
from waste_collection_schedule.exceptions import (
    SourceArgumentNotFoundWithSuggestions,
    SourceArgumentRequired,
)

WEEKDAYS = {
    "monday": MO,
    "tuesday": TU,
    "wednesday": WE,
    "thursday": TH,
    "friday": FR,
    "saturday": SA,
    "sunday": SU,
}

TITLE = "Thurrock"
DESCRIPTION = "Source for Thurrock."
URL = "https://www.thurrock.gov.uk/"
TEST_CASES = {
    "Camden Close Chadwell St Mary": {
        "street": "Camden Close",
        "town": "Chadwell St Mary",
    },
    "Abberton Way West Thurrock (street starting with A)": {
        "street": "Abberton Way",
        "town": "West Thurrock",
    },
}


ICON_MAP = {
    "Brown": Icons.ORGANIC,
    "Blue": Icons.PAPER,
    "Green": Icons.RECYCLING,
    "Grey": Icons.RECYCLING,
    "Green/Grey": Icons.RECYCLING,
}


# Streets beginning with A use the base URL (no letter suffix).
# All other letters append "-<letter>" to the base URL.
STREETS_BASE_URL = (
    "https://www.thurrock.gov.uk/household-bin-collection-days/street-names"
)
# /bindays redirects to the current weeks table page.
API_URL = "https://www.thurrock.gov.uk/bindays"
API_URL_FALLBACK = (
    "https://www.thurrock.gov.uk/household-bin-collection-days/"
    "general-residential-waste-and-recycling-collections"
)

# Matches both ASCII hyphen-minus (-) and Unicode en-dash (–) with optional surrounding whitespace.
DATE_RANGE_RE = re.compile(r"\s*[-–—]\s*")
# Matches " and " or " / " (with optional extra whitespace) as bin-type separators.
BIN_SPLIT_RE = re.compile(r"\s*/\s*|\s+and\s+")

# How many extra fortnights to project when the council table is short.
PREDICT_WEEKS = 12
# Minimum number of future collection *days* we want after today.
MIN_FUTURE_DAYS = 4


class Source:
    def __init__(self, street: str, town: str):
        self._street: str = street
        self._town: str = town
        self._day: weekday | None = None
        self._round: str | None = None

    def _streets_url(self) -> str:
        first = self._street[0].lower()
        if first == "a":
            return STREETS_BASE_URL
        return f"{STREETS_BASE_URL}-{first}"

    def fetch_day(self):
        if len(self._street) == 0:
            raise SourceArgumentRequired(
                "street",
                "Please provide a street name",
            )
        r = requests.get(self._streets_url(), verify=False)
        r.raise_for_status()
        soup = BeautifulSoup(
            r.text.replace("&nbsp;", " ").replace("\xa0", " "), "html.parser"
        )
        table = soup.select_one("table")
        if not table:
            raise Exception("street, town Table not found")
        towns = []
        streets = []
        day_str = None
        town_match = False
        street_match = False

        for row in table.select("tr")[1:]:
            cells = row.select("td")
            if len(cells) != 3:
                continue
            # Use rsplit to handle street names that contain ", " (e.g. parenthetical notes)
            parts = cells[0].text.strip().rsplit(", ", 1)
            if len(parts) != 2:
                continue
            street, town = parts

            towns.append(town.strip().casefold())
            streets.append(street.strip().casefold())
            if self._street.casefold() in street.casefold():
                street_match = True
            if self._town.casefold() in town.casefold():
                town_match = True
            if street_match and town_match:
                day_str = cells[1].text.strip()
                self._round = cells[2].text.strip()
                break
        if not day_str:
            if town_match:
                raise SourceArgumentNotFoundWithSuggestions(
                    "street",
                    self._street,
                    streets,
                )
            raise SourceArgumentNotFoundWithSuggestions(
                "town",
                self._town,
                towns,
            )

        if day_str.lower() not in WEEKDAYS:
            raise Exception(f"Day ({day_str}) not a valid weekday")
        self._day = WEEKDAYS[day_str.lower()]

    def parse_date_range(self, range_str: str) -> tuple[date, date]:
        """Parse a date range such as '18 May - 22 May' or '25 May – 29 May'."""
        now = datetime.now()
        parts = DATE_RANGE_RE.split(range_str.strip())
        if len(parts) != 2:
            raise ValueError(f"Cannot parse date range: {range_str!r}")
        start_str, end_str = parts
        start_date = datetime.strptime(
            start_str.strip() + f" {now.year}", "%d %B %Y"
        ).date()
        end_date = datetime.strptime(
            end_str.strip() + f" {now.year}", "%d %B %Y"
        ).date()
        # Handle a range that straddles a year boundary (e.g. 30 Dec - 3 Jan)
        if start_date.month == 12 and end_date.month == 1:
            end_date = end_date.replace(year=start_date.year + 1)
        # If both ends are far in the past relative to today (e.g. table still
        # showing last calendar year in January), bump to next year.
        if end_date < now.date() - timedelta(days=180):
            start_date = start_date.replace(year=start_date.year + 1)
            end_date = end_date.replace(year=end_date.year + 1)
        return start_date, end_date

    @staticmethod
    def _normalise_bin_label(bin_text: str) -> str:
        """Collapse 'Green / Grey' style labels into a single type name."""
        text = " ".join(bin_text.split())
        # Prefer the combined label when both green and grey appear together.
        lower = text.casefold()
        if "green" in lower and "grey" in lower:
            return "Green/Grey"
        if "green" in lower and "gray" in lower:
            return "Green/Grey"
        return text

    def _bins_from_cell(self, bin_text: str) -> list[str]:
        normalised = self._normalise_bin_label(bin_text)
        if normalised == "Green/Grey":
            return ["Green/Grey"]
        return [b.strip() for b in BIN_SPLIT_RE.split(bin_text) if b.strip()]

    def _entries_for_week(
        self, start_date: date, end_date: date, bins: list[str]
    ) -> list[Collection]:
        assert self._day is not None
        entries: list[Collection] = []
        for bin_type in bins:
            for col_date in rrule(
                WEEKLY,
                dtstart=start_date,
                until=end_date,
                byweekday=self._day,
            ):
                entries.append(
                    Collection(
                        date=col_date.date(),
                        t=bin_type,
                        icon=ICON_MAP.get(bin_type),
                    )
                )
        return entries

    def _predict_forward(
        self,
        last_start: date,
        last_end: date,
        last_bins: list[str],
        prev_bins: list[str] | None,
    ) -> list[Collection]:
        """
        Extend the alternating fortnightly pattern beyond the published table.

        Thurrock alternates Round A / Round B bin sets each Mon-Fri week.
        When the council page only lists a short rolling window, sensors would
        otherwise go unknown once that window is exhausted.
        """
        assert self._day is not None
        entries: list[Collection] = []

        # Determine the two alternating bin sets.
        set_a = last_bins
        set_b = prev_bins if prev_bins else self._alternate_bins(last_bins)

        week_start = last_start + timedelta(days=7)
        week_end = last_end + timedelta(days=7)
        use_a = False  # next week after last published uses the other set

        for _ in range(PREDICT_WEEKS):
            bins = set_b if use_a is False else set_a
            # After first predicted week, swap which set is "current".
            # use_a False -> set_b (alternate of last), then True -> set_a, etc.
            if use_a:
                bins = set_a
            else:
                bins = set_b
            entries.extend(self._entries_for_week(week_start, week_end, bins))
            week_start += timedelta(days=7)
            week_end += timedelta(days=7)
            use_a = not use_a

        return entries

    @staticmethod
    def _alternate_bins(bins: list[str]) -> list[str]:
        """Guess the other fortnight's bins when only one week is known."""
        joined = " ".join(bins).casefold()
        if "blue" in joined or "brown" in joined:
            return ["Green/Grey"]
        return ["Blue", "Brown"]

    def _load_weeks_table(self) -> BeautifulSoup:
        last_error: Exception | None = None
        for url in (API_URL, API_URL_FALLBACK):
            try:
                r = requests.get(url, verify=False, timeout=30)
                r.raise_for_status()
                soup = BeautifulSoup(r.text.replace("\xa0", " "), "html.parser")
                if soup.select_one("table"):
                    return soup
            except Exception as exc:  # noqa: BLE001 - try fallback URL
                last_error = exc
        if last_error:
            raise last_error
        raise Exception("Collection table not found")

    def fetch(self) -> list[Collection]:
        if self._day is None or self._round is None:
            self.fetch_day()
            assert self._day is not None
            assert self._round is not None

        soup = self._load_weeks_table()
        table = soup.select_one("table")
        if not table:
            raise Exception("Collection table not found")

        entries: list[Collection] = []
        week_rows: list[tuple[date, date, list[str]]] = []

        for tr in table.select("tr")[1:]:
            cells = tr.select("td")
            if len(cells) != 3:
                # Skip malformed / holiday note rows instead of aborting.
                continue
            try:
                start_date, end_date = self.parse_date_range(cells[0].text.strip())
            except ValueError:
                continue

            bin_text = cells[(1 if self._round == "A" else 2)].text.strip()
            bins = self._bins_from_cell(bin_text)
            if not bins:
                continue
            week_rows.append((start_date, end_date, bins))
            entries.extend(self._entries_for_week(start_date, end_date, bins))

        if not week_rows:
            raise Exception("No collection weeks parsed from council table")

        today = date.today()
        future_dates = {e.date for e in entries if e.date >= today}

        # Council often only publishes ~6-8 weeks. Project the alternating
        # pattern so sensors do not go unknown when the table rolls off.
        if len(future_dates) < MIN_FUTURE_DAYS:
            week_rows.sort(key=lambda w: w[0])
            last_start, last_end, last_bins = week_rows[-1]
            prev_bins = week_rows[-2][2] if len(week_rows) >= 2 else None
            predicted = self._predict_forward(
                last_start, last_end, last_bins, prev_bins
            )
            # Avoid duplicates with already-parsed dates.
            existing = {(e.date, e.type) for e in entries}
            for e in predicted:
                if (e.date, e.type) not in existing:
                    entries.append(e)

        return entries
