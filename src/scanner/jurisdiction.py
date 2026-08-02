"""Offline, explainable high-risk jurisdiction location screening.

This classifies self-published GitHub profile locations.  It does not infer
nationality, citizenship, ethnicity, legal registration, or intent.  Runtime
uses a packaged GeoNames-derived gazetteer and performs no network requests.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Iterable, Literal

from .models import RepoData


CountryCode = Literal["RU", "IR", "KP"]
SubjectRole = Literal["owner", "top_contributor", "contributor_organization"]

COUNTRY_NAMES: dict[CountryCode, str] = {
    "RU": "Russia",
    "IR": "Iran",
    "KP": "North Korea",
}
COUNTRY_ALIASES: dict[CountryCode, set[str]] = {
    "RU": {
        "russia", "russian federation", "rossiya", "rossija",
        "rossiyskaya federatsiya", "россия", "российская федерация", "рф",
    },
    "IR": {
        "iran", "islamic republic of iran", "iran islamic republic of",
        "jomhuri ye eslami ye iran", "ایران", "جمهوری اسلامی ایران",
    },
    "KP": {
        "north korea", "democratic people s republic of korea", "dprk",
        "korea democratic people s republic of", "조선민주주의인민공화국", "북한",
    },
}
FLAG_EMOJI: dict[str, CountryCode] = {"🇷🇺": "RU", "🇮🇷": "IR", "🇰🇵": "KP"}
CURATED_PLACES: dict[str, tuple[CountryCode, str]] = {
    "spb": ("RU", "Saint Petersburg"),
    "piter": ("RU", "Saint Petersburg"),
    "st petersburg": ("RU", "Saint Petersburg"),
    "saint petersburg": ("RU", "Saint Petersburg"),
    "sankt petersburg": ("RU", "Saint Petersburg"),
    "санкт петербург": ("RU", "Saint Petersburg"),
    "siberia": ("RU", "Siberia"),
    "сибирь": ("RU", "Siberia"),
    "ekaterinburg": ("RU", "Yekaterinburg"),
    "омск": ("RU", "Omsk"),
    "ussuriisk": ("RU", "Ussuriysk"),
    "petushki": ("RU", "Petushki"),
    "оренбург": ("RU", "Orenburg"),
    "москва": ("RU", "Moscow"),
    "chuvash republic": ("RU", "Chuvash Republic"),
    "nizhny novgorod": ("RU", "Nizhny Novgorod"),
    "vladimir": ("RU", "Vladimir"),
}
US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}
# A denial is not a declaration. Profiles say "not north korea", "def not
# DPRK", "north korea but no" — the country is named in order to disown it, and
# reading that as self-declared presence is the plainest error this classifier
# can make. Counted only inside the *same segment* as the match, so an unrelated
# "No. 5 Lenin St, Moscow, Russia" is untouched.
NEGATION_TOKENS = {
    "no", "not", "non", "isnt", "arent", "aint", "never", "nope", "fake", "ex",
    "нет", "не", "아니", "아님",
}
# Places outside the policy scope whose presence makes a policy match
# ambiguous, in the same way another country name already does. The gazetteer
# only carries Russia/Iran/North Korea, so without this a profile reading
# "Seoul, North Korea" or "Beijing / Pyongyang" has no visible conflict at all
# and scores as a confident declaration. Capitals, megacities and common tech
# hubs only; nothing here may name a place inside RU/IR/KP.
FOREIGN_PLACES = {
    "seoul", "busan", "incheon", "daegu", "daejeon", "gwangju", "jeju",
    "tokyo", "osaka", "kyoto", "yokohama", "nagoya", "fukuoka", "sapporo",
    "beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou", "chengdu",
    "wuhan", "nanjing", "xian", "tianjin", "chongqing", "suzhou", "qingdao",
    "hong kong", "hongkong", "macau", "taipei", "kaohsiung", "taichung",
    "singapore", "kuala lumpur", "jakarta", "bangkok", "manila", "hanoi",
    "ho chi minh city", "saigon", "phnom penh", "vientiane", "yangon",
    "new delhi", "delhi", "new dehli", "dehli", "mumbai", "bangalore",
    "bengaluru", "hyderabad", "chennai", "kolkata", "pune", "ahmedabad",
    "karachi", "lahore", "islamabad", "dhaka", "colombo", "kathmandu",
    "london", "manchester", "birmingham", "edinburgh", "glasgow", "dublin",
    "paris", "lyon", "marseille", "toulouse", "berlin", "munich", "hamburg",
    "frankfurt", "cologne", "stuttgart", "dusseldorf", "leipzig", "dresden",
    "amsterdam", "rotterdam", "brussels", "antwerp", "luxembourg",
    "madrid", "barcelona", "valencia", "seville", "lisbon", "porto",
    "rome", "milan", "naples", "turin", "florence", "venice",
    "zurich", "geneva", "bern", "basel", "vienna", "salzburg", "graz",
    "stockholm", "gothenburg", "oslo", "bergen", "copenhagen", "aarhus",
    "helsinki", "tallinn", "riga", "vilnius", "warsaw", "krakow", "wroclaw",
    "prague", "brno", "bratislava", "budapest", "bucharest", "sofia",
    "athens", "thessaloniki", "belgrade", "zagreb", "ljubljana", "sarajevo",
    "kyiv", "kiev", "lviv", "odesa", "odessa", "kharkiv", "dnipro",
    "minsk", "chisinau", "tbilisi", "yerevan", "baku", "istanbul", "ankara",
    "izmir", "almaty", "astana", "tashkent", "bishkek", "dushanbe",
    "new york", "brooklyn", "manhattan", "san francisco", "los angeles",
    "seattle", "boston", "chicago", "austin", "denver", "portland",
    "san diego", "san jose", "atlanta", "miami", "houston", "dallas",
    "philadelphia", "phoenix", "las vegas", "washington dc", "silicon valley",
    "toronto", "vancouver", "montreal", "ottawa", "calgary", "quebec",
    "mexico city", "guadalajara", "monterrey", "bogota", "medellin",
    "lima", "santiago", "buenos aires", "montevideo", "asuncion", "quito",
    "sao paulo", "rio de janeiro", "brasilia", "curitiba", "porto alegre",
    "cairo", "alexandria", "casablanca", "rabat", "tunis", "algiers",
    "lagos", "abuja", "accra", "nairobi", "kampala", "addis ababa",
    "johannesburg", "cape town", "durban", "pretoria",
    "dubai", "abu dhabi", "doha", "riyadh", "jeddah", "kuwait city",
    "manama", "muscat", "amman", "beirut", "baghdad", "tel aviv",
    "jerusalem", "haifa", "sydney", "melbourne", "brisbane", "perth",
    "adelaide", "canberra", "auckland", "wellington", "christchurch",
}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",
}
SEGMENT_SPLIT = re.compile(r"[,;/|()\[\]·•]+")


@dataclass(frozen=True)
class LocationMatch:
    country_code: CountryCode
    country: str
    confidence: Literal["high", "review"]
    match_kind: Literal["explicit_country", "region", "place"]
    matched: str
    reason: str


@dataclass(frozen=True)
class Exposure:
    role: SubjectRole
    subject: str
    country_code: CountryCode
    country: str
    match_kind: str


@dataclass(frozen=True)
class Assessment:
    assessed_locations: int
    exposures: tuple[Exposure, ...]
    review_matches: int


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _terms(value: str) -> set[str]:
    normalized = normalize(value)
    tokens = normalized.split()
    result = {normalized}
    for size in range(1, min(5, len(tokens)) + 1):
        result.update(" ".join(tokens[index:index + size]) for index in range(len(tokens) - size + 1))
    result.update(normalize(segment) for segment in SEGMENT_SPLIT.split(value) if normalize(segment))
    return {term for term in result if len(term) >= 3}


@lru_cache(maxsize=1)
def _gazetteer() -> dict:
    resource = files("scanner").joinpath("data/jurisdiction_places.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    data["_region_sets"] = {code: set(values) for code, values in data["regions"].items()}
    data["_country_name_to_code"] = {
        country["name"]: country["code"] for country in data["countries"]
    }
    data["_country_segment_to_code"] = {
        segment: country["code"]
        for country in data["countries"]
        for segment in (country["code"].casefold(), country["iso3"].casefold())
    }
    return data


def _negated(location: str, matched: str) -> bool:
    """Does the profile name the country in order to deny being there?

    Scoped to the segment carrying the match. A negation elsewhere in the field
    is about something else — "No. 5 Lenin St, Moscow, Russia" declares Russia
    and denies nothing.
    """
    needle = normalize(matched)
    for segment in SEGMENT_SPLIT.split(location):
        normalized = normalize(segment)
        if not normalized:
            continue
        # A flag emoji leaves nothing behind under normalization, so fall back
        # to the raw segment to find the one the match came from.
        carries = needle in normalized if needle else matched in segment
        if carries and set(normalized.split()) & NEGATION_TOKENS:
            return True
    return False


def _conflicting_context(location: str, target: CountryCode, gazetteer: dict) -> bool:
    terms = _terms(location)
    segments = {normalize(segment) for segment in SEGMENT_SPLIT.split(location) if normalize(segment)}
    for name in terms:
        code = gazetteer["_country_name_to_code"].get(name)
        if code is not None and code != target:
            return True
    for segment in segments:
        code = gazetteer["_country_segment_to_code"].get(segment)
        if code is not None and code != target:
            return True
    # A named place outside the policy scope is the same kind of evidence as a
    # named foreign country, and the gazetteer cannot supply it: it holds only
    # policy-country places, so "Seoul" beside "North Korea" would otherwise
    # read as unopposed. US state names and codes stay segment-matched — several
    # of the codes ("in", "or", "me") are ordinary words and only a whole
    # segment makes them evidence.
    if terms & FOREIGN_PLACES:
        return True
    return bool(segments & (US_STATE_CODES | US_STATE_NAMES))


def classify_location(location: str | None) -> LocationMatch | None:
    """Return the strongest country match, or ``None`` for no evidence."""
    if not isinstance(location, str) or not location.strip():
        return None
    gazetteer = _gazetteer()
    terms = _terms(location)
    segments = {normalize(segment) for segment in SEGMENT_SPLIT.split(location) if normalize(segment)}

    explicit: list[tuple[CountryCode, str]] = []
    for emoji, code in FLAG_EMOJI.items():
        if emoji in location:
            explicit.append((code, emoji))
    for code, aliases in COUNTRY_ALIASES.items():
        matched = next((alias for alias in aliases if normalize(alias) in terms), None)
        if matched:
            explicit.append((code, matched))
        if code.casefold() in segments:
            explicit.append((code, code))
    explicit_codes = {code for code, _ in explicit}
    if explicit:
        code, matched = explicit[0]
        if _negated(location, matched):
            return LocationMatch(
                code, COUNTRY_NAMES[code], "review", "explicit_country", matched,
                "the profile names the country in order to deny being there",
            )
        conflict = len(explicit_codes) > 1 or _conflicting_context(location, code, gazetteer)
        return LocationMatch(
            code, COUNTRY_NAMES[code], "review" if conflict else "high",
            "explicit_country", matched,
            "country is self-declared but another country/state is also present" if conflict
            else "profile location explicitly names the country",
        )

    candidates: list[LocationMatch] = []
    for term in terms:
        curated = CURATED_PLACES.get(term)
        if curated:
            code, canonical = curated
            candidates.append(
                LocationMatch(code, COUNTRY_NAMES[code], "high", "place", term,
                              f"curated geographic alias for {canonical}")
            )
    for code, region_aliases in gazetteer["_region_sets"].items():
        matched = next((term for term in terms if term in region_aliases and term in segments), None)
        if matched:
            candidates.append(
                LocationMatch(code, COUNTRY_NAMES[code], "high", "region", matched,
                              "exact self-declared administrative region")
            )
    for term in terms:
        for place in gazetteer["places"].get(term, []):
            code = place["country"]
            candidates.append(
                LocationMatch(code, COUNTRY_NAMES[code], "high", "place", term,
                              f"GeoNames place alias is {place['basis']} for this country")
            )
    if not candidates:
        return None
    # Prefer an explicit segment/longer match, then the largest place embedded
    # in the gazetteer ordering. Conflicting country context always downgrades.
    candidates.sort(key=lambda item: (item.match_kind == "region", len(item.matched)), reverse=True)
    best = candidates[0]
    if _negated(location, best.matched):
        return LocationMatch(
            best.country_code, best.country, "review", best.match_kind, best.matched,
            "the profile names the place in order to deny being there",
        )
    candidate_countries = {candidate.country_code for candidate in candidates}
    conflict = len(candidate_countries) > 1 or _conflicting_context(location, best.country_code, gazetteer)
    if conflict:
        return LocationMatch(
            best.country_code, best.country, "review", best.match_kind, best.matched,
            "place-name match conflicts with another country/state in the location",
        )
    return best


def _located_subjects(data: RepoData) -> Iterable[tuple[SubjectRole, str, str | None]]:
    if data.owner is not None:
        yield "owner", data.owner.login, data.owner.location
    for contributor in data.maintainership.top_contributors:
        if contributor.profile is None:
            continue
        yield "top_contributor", contributor.login, contributor.profile.location
        for organization in contributor.profile.organizations:
            yield "contributor_organization", organization.login, organization.location


def assess_repo(data: RepoData) -> Assessment:
    assessed = 0
    review = 0
    exposures: set[Exposure] = set()
    for role, subject, location in _located_subjects(data):
        if not isinstance(location, str) or not location.strip():
            continue
        assessed += 1
        match = classify_location(location)
        if match is None:
            continue
        if match.confidence != "high":
            review += 1
            continue
        exposures.add(Exposure(role, subject.casefold(), match.country_code, match.country, match.match_kind))
    return Assessment(
        assessed,
        tuple(sorted(exposures, key=lambda item: (item.role, item.country_code, item.subject))),
        review,
    )
