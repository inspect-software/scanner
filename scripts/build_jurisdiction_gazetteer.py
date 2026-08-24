"""Build the compact runtime gazetteer used by jurisdiction screening.

Source: GeoNames country dumps, cities500 and countryInfo (CC BY 4.0).
The scanner never downloads these files at runtime; this maintainer script
turns them into a small deterministic JSON package resource.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path


# Anchored on the project root — the directory holding pyproject.toml — rather
# than on the workspace that used to contain it. `parents[1]` is that directory
# both here and in the standalone scanner repository, where there is no
# `scanner/` path segment to walk through.
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".gazetteer-cache"
OUTPUT = ROOT / "src" / "scanner" / "data" / "jurisdiction_places.json"
BASE = "https://download.geonames.org/export/dump"
TARGETS = {"RU": "Russia", "IR": "Iran", "KP": "North Korea"}
EXCLUDED_UKRAINIAN_TERRITORIES = {
    "crimea", "krym", "sevastopol", "donetsk", "luhansk", "lugansk",
    "zaporizhzhia", "zaporozhye", "kherson", "mariupol",
    "крим", "севастополь", "донецьк", "донецк", "луганськ", "луганск",
    "запоріжжя", "запорожье", "херсон", "маріуполь", "мариуполь",
}
GENERIC = {
    "central", "city", "east", "home", "north", "online", "remote",
    "south", "west", "world", "worldwide",
}


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def aliases(row: list[str], *, include_alternates: bool = True) -> set[str]:
    values = [row[1], row[2]]
    if include_alternates and row[3]:
        values.extend(row[3].split(","))
    return {
        alias
        for value in values
        if (alias := normalize(value))
        and len(alias) >= 4
        and alias not in GENERIC
        and not alias.isdecimal()
    }


def runtime_aliases(row: list[str]) -> set[str]:
    """Keep the canonical/native and ASCII names plus a few short aliases.

    GeoNames carries hundreds of historic and language-specific aliases for
    some places. Shipping all of them multiplies package memory for almost no
    scanner value. Short aliases are the forms people realistically put in a
    GitHub location field.
    """
    primary = aliases(row, include_alternates=False)
    alternates = aliases(row) - primary
    useful = sorted(
        (value for value in alternates if len(value) <= 32 and len(value.split()) <= 4),
        key=lambda value: (len(value), value),
    )[:2]
    return primary | set(useful)


def download(name: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if not path.exists():
        urllib.request.urlretrieve(f"{BASE}/{name}", path)
    return path


def unzip(name: str, member: str) -> Path:
    archive = download(name)
    path = CACHE / member
    if not path.exists():
        with zipfile.ZipFile(archive) as source:
            source.extract(member, CACHE)
    return path


def rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = line.rstrip("\n").split("\t")
            if len(row) >= 19:
                yield row


def main() -> None:
    country_paths = {code: unzip(f"{code}.zip", f"{code}.txt") for code in TARGETS}
    world_path = unzip("cities500.zip", "cities500.txt")
    country_info = download("countryInfo.txt")

    target_places: list[dict] = []
    candidate_aliases: set[str] = set()
    regions: dict[str, set[str]] = defaultdict(set)
    for code, path in country_paths.items():
        for row in rows(path):
            feature_class, feature_code = row[6], row[7]
            population = int(row[14] or 0)
            row_aliases = runtime_aliases(row)
            if code == "RU" and row_aliases & EXCLUDED_UKRAINIAN_TERRITORIES:
                continue
            if feature_class == "P" and population >= 20_000:
                target_places.append(
                    {"country": code, "canonical": row[1], "population": population, "aliases": row_aliases}
                )
                candidate_aliases.update(row_aliases)
            elif feature_code in {"ADM1", "ADM2"}:
                regions[code].update(
                    alias for alias in aliases(row, include_alternates=False) if len(alias) >= 5
                )

    world: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows(world_path):
        if row[6] != "P":
            continue
        population = int(row[14] or 0)
        for alias in aliases(row) & candidate_aliases:
            code = row[8]
            world[alias][code] = max(world[alias].get(code, 0), population)

    accepted: dict[str, list[dict]] = defaultdict(list)
    for place in target_places:
        code = place["country"]
        for alias in place.pop("aliases"):
            populations = world.get(alias, {})
            own = max(place["population"], populations.get(code, 0))
            other = max((value for other_code, value in populations.items() if other_code != code), default=0)
            unique = not populations or set(populations) == {code}
            dominant = own >= 100_000 and own >= max(1, other) * 10
            if unique or dominant:
                accepted[alias].append(
                    {
                        "country": code,
                        "canonical": place["canonical"],
                        "population": own,
                        "kind": "place",
                        "basis": "unique" if unique else "dominant",
                    }
                )

    countries = []
    with country_info.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            row = line.rstrip("\n").split("\t")
            if len(row) >= 5:
                countries.append({"code": row[0], "iso3": row[1], "name": normalize(row[4])})

    payload = {
        "format": "inspect-jurisdiction-gazetteer-v1",
        "source": "GeoNames dump",
        "source_url": BASE + "/",
        "license": "CC BY 4.0",
        "target_countries": TARGETS,
        "countries": countries,
        "places": {key: value for key, value in sorted(accepted.items())},
        "regions": {code: sorted(values) for code, values in sorted(regions.items())},
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "bytes": OUTPUT.stat().st_size,
                "place_aliases": len(payload["places"]),
                "region_aliases": sum(len(value) for value in payload["regions"].values()),
            }
        )
    )


if __name__ == "__main__":
    main()
