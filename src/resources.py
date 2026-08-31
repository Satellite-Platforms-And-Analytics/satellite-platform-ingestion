"""
Satellite Platform — WIT Resource Bridge
=========================================
Connects the satellite ingestion pipeline to the WIT knowledge base.

Usage inside the pipeline:
    from src.resources import SatelliteResources

    with SatelliteResources() as res:
        tle_sources  = res.tle_sources()
        agencies     = res.space_agencies()
        found        = res.find("conjunction analysis tools")
        print(f"{len(tle_sources)} TLE sources available")

Or import the module-level convenience functions:
    from src.resources import tle_sources, find, space_agencies
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Dict, Optional, Any

# ── Locate WIT ────────────────────────────────────────────────────────────────
# Reads WIT_PATH from environment; falls back to D:\Projects\WIT
_WIT_PATH = Path(os.environ.get("WIT_PATH",
                                r"D:\Projects\WIT"))
_WIT_BASE  = Path(os.environ.get("WIT_BASE_DIR",
                                 r"D:\Databases\wit"))


def _import_wit():
    """Import WebIntelligence, adding WIT to sys.path if needed."""
    if str(_WIT_PATH) not in sys.path:
        sys.path.insert(0, str(_WIT_PATH))
    try:
        from wit import WebIntelligence
        return WebIntelligence
    except ImportError as e:
        raise RuntimeError(
            f"WIT package not found at {_WIT_PATH}.\n"
            f"Set WIT_PATH in your .env to the correct location.\n"
            f"Original error: {e}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Resource categories aligned with the satellite pipeline's modules
# ══════════════════════════════════════════════════════════════════════════════

# Maps pipeline concern → WIT search terms / tags / domains
_RESOURCE_MAP = {
    "tle_sources": {
        "subdomains": ["Orbital Mechanics", "Satellite Tracking",
                       "Space Object Tracking"],
        "search":     ["TLE two-line element orbital elements NORAD",
                       "celestrak space-track satellite tracking"],
        "terms":      ["TLE", "NORAD", "celestrak", "space-track",
                       "two-line element", "orbital element", "ephemeris"],
    },
    "tracking_databases": {
        # "Database" also holds industry directories and job boards - too broad.
        "subdomains": ["Satellite Tracking", "Space Object Tracking"],
        "search":     ["satellite tracking SSA conjunction analysis space fence"],
        "terms":      ["SSA", "conjunction", "space fence", "satellite catalog",
                       "orbital debris", "space surveillance"],
    },
    "imagery_sources": {
        "subdomains": [],
        "search":     ["satellite imagery remote sensing earth observation API"],
        "terms":      ["SAR", "remote sensing", "earth observation",
                       "satellite imagery", "multispectral", "hyperspectral"],
    },
    "launch_providers": {
        "subdomains": ["Launch Schedules", "Private Spaceflight"],
        "search":     ["rocket launch provider smallsat rideshare manifests"],
        # "rocket" alone matched every NASA centre whose blurb mentions
        # rockets - use named providers and multi-word phrases instead.
        "terms":      ["launch vehicle", "rideshare", "launch schedule",
                       "launch provider", "rocket lab", "spacex", "arianespace",
                       "blue origin", "united launch alliance"],
    },
    "industry_news": {
        "subdomains": ["News", "Space News"],
        "search":     ["satellite aerospace news space industry publication"],
        "terms":      ["spacenews", "aviationweek", "parabolicarc",
                       "spaceflight", "nasaspaceflight"],
        "domains":    ["News & Media"],
    },
    "regulatory_sources": {
        "subdomains": ["Standards", "Space Policy", "Space Agency"],
        "search":     ["satellite regulatory FCC ITU frequency coordination ITAR"],
        "terms":      ["FCC", "ITU", "ITAR", "EAR", "spectrum allocation",
                       "frequency coordination", "export control", "licensing"],
        "domains":    ["Government & Policy"],
    },
    "operators": {
        "subdomains": ["Private Spaceflight"],
        "search":     ["satellite operator constellation GEO LEO MEO commercial"],
        "terms":      ["constellation", "telesat", "intelsat", "viasat",
                       "iridium", "starlink", "oneweb", "satellite operator"],
    },
    "funding_sources": {
        "subdomains": ["Science Funding", "Government Funding",
                       "Government Subsidies"],
        "search":     ["SBIR STTR DoD space funding grants contracts awards"],
        "terms":      ["SBIR", "STTR", "DARPA", "OTA", "SAM.gov", "USASpending",
                       "grant program", "contract award"],
        "domains":    ["Government & Policy"],
    },
    "standards": {
        "subdomains": ["Standards"],
        "search":     ["CCSDS space data standard ECSS AIAA MIL-SPEC"],
        "terms":      ["CCSDS", "ECSS", "AIAA", "MIL-SPEC", "ICD",
                       "interface control"],
    },
    "trl_mrl": {
        "subdomains": [],
        "search":     ["technology readiness TRL manufacturing readiness MRL assessment"],
        "terms":      ["TRL", "MRL", "technology readiness", "manufacturing readiness"],
        "domains":    ["Science & Engineering"],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# Main class
# ══════════════════════════════════════════════════════════════════════════════

class SatelliteResources:
    """
    Resource intelligence layer for the satellite platform.

    Queries the WIT knowledge base and returns curated web resources
    organized around the satellite pipeline's modules.

    Example:
        with SatelliteResources() as res:
            for src in res.tle_sources():
                print(src["url"], src["name"])
    """

    def __init__(self):
        WebIntelligence = _import_wit()
        self._wi = WebIntelligence()
        self._cache: Dict[str, List[Dict]] = {}

    def close(self):
        self._wi.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Core search ───────────────────────────────────────────────────────────

    def find(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Full-text search across the WIT knowledge base.
        Returns sites sorted by relevance.

        Example:
            res.find("conjunction analysis probability of collision")
        """
        return self._wi.search(query, limit=limit)

    def get_satellite_sites(self, limit: int = 500) -> List[Dict]:
        """All sites in the satellite project collection."""
        return self._wi.get_collection("satellite")

    def get_space_sites(self, subdomain: str = None) -> List[Dict]:
        """All Space & Aerospace sites, optionally filtered by subdomain."""
        return self._wi.get_sites(
            km_domain="Space & Aerospace",
            km_subdomain=subdomain,
        )

    # ── Pipeline-aligned resource getters ────────────────────────────────────

    def tle_sources(self) -> List[Dict]:
        """
        Authoritative TLE and orbital element data sources.
        Used by: src/tle/fetcher.py
        """
        return self._query("tle_sources")

    def tracking_databases(self) -> List[Dict]:
        """
        Satellite tracking databases, SSA tools, conjunction analysis.
        Used by: src/tracking/
        """
        return self._query("tracking_databases")

    def imagery_sources(self) -> List[Dict]:
        """
        Satellite imagery and remote sensing data sources.
        Used by: src/imagery/ingest.py
        """
        return self._query("imagery_sources")

    def launch_providers(self) -> List[Dict]:
        """
        Launch vehicle providers, manifests, rideshare options.
        Used by: src/launches/
        """
        return self._query("launch_providers")

    def industry_news(self) -> List[Dict]:
        """
        Satellite and aerospace industry news sources.
        Used by: src/news/
        """
        return self._query("industry_news")

    def regulatory_sources(self) -> List[Dict]:
        """
        Regulatory bodies, frequency coordination, ITAR/EAR resources.
        Used by: src/regulatory/
        """
        return self._query("regulatory_sources")

    def operators(self) -> List[Dict]:
        """
        Satellite operators and constellation data sources.
        Used by: src/operators/
        """
        return self._query("operators")

    def funding_sources(self) -> List[Dict]:
        """SBIR, STTR, DoD contracts, and other funding resources."""
        return self._query("funding_sources")

    def standards(self) -> List[Dict]:
        """CCSDS, ECSS, AIAA and other applicable standards."""
        return self._query("standards")

    def trl_mrl_resources(self) -> List[Dict]:
        """TRL/MRL assessment tools and references."""
        return self._query("trl_mrl")

    # ── Entity intelligence ───────────────────────────────────────────────────

    def space_agencies(self) -> List[Dict]:
        """
        Government space agencies (NASA, ESA, JAXA, etc.).
        Returns entity records with website URLs and descriptions.
        """
        return self._wi.get_entities(entity_type="government")

    def satellite_companies(self) -> List[Dict]:
        """Commercial satellite and aerospace companies."""
        return self._wi.get_entities(entity_type="company")

    def get_entity_relationships(self, name: str) -> List[Dict]:
        """
        Get known relationships for a company or agency.
        Example: res.get_entity_relationships("SpaceX")
        """
        return self._wi.get_relationships(name)

    def get_knowledge_graph(self) -> Dict:
        """
        Full knowledge graph for the satellite project.
        Returns {nodes: [...], edges: [...]} for visualization.
        """
        return self._wi.get_knowledge_graph(project="satellite")

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync(self, verbose: bool = True) -> Dict[str, int]:
        """
        Populate the satellite WIT project from Space & Aerospace sites.
        Safe to run repeatedly — skips sites already in the project.

        Returns counts of added, skipped, failed.
        """
        counts = {"added": 0, "skipped": 0, "failed": 0}
        space_sites = self._wi.get_sites(km_domain="Space & Aerospace")
        defense_sites = self._wi.get_sites(km_domain="Defense & Intelligence")
        gov_sites    = self._wi.get_sites(km_domain="Government & Policy")

        all_sites = {s["url"]: s for s in space_sites + defense_sites + gov_sites}

        for url, site in all_sites.items():
            try:
                existing = self._wi.get_site_projects(url)
                if "satellite" in existing:
                    counts["skipped"] += 1
                else:
                    self._wi.add_to_project(url, "satellite",
                                            notes=f"Auto-synced: {site.get('km_subdomain','')}")
                    counts["added"] += 1
            except Exception:
                counts["failed"] += 1

        if verbose:
            print(f"  Satellite sync: +{counts['added']} added, "
                  f"{counts['skipped']} already present, "
                  f"{counts['failed']} failed")
        return counts

    def summary(self) -> Dict[str, Any]:
        """Quick summary of available satellite resources."""
        return {
            "total_wit_sites":      self._wi.stats()["total_sites"],
            "space_sites":          len(self.get_space_sites()),
            "satellite_project":    len(self.get_satellite_sites()),
            "tle_sources":          len(self.tle_sources()),
            "tracking_databases":   len(self.tracking_databases()),
            "launch_providers":     len(self.launch_providers()),
            "regulatory_sources":   len(self.regulatory_sources()),
            "funding_sources":      len(self.funding_sources()),
            "entities":             len(self.space_agencies()) + len(self.satellite_companies()),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    # Domains that are relevant to space/satellite work
    _SPACE_DOMAINS = frozenset([
        "Space & Aerospace", "Science & Engineering",
        "Defense & Intelligence", "Government & Policy",
        "Technology & Computing", "Research & Academic",
        "News & Media",
    ])

    @staticmethod
    def _is_acronym(term: str) -> bool:
        """
        True for short all-caps tokens (EAR, ITU, TRL, SBIR).

        These must match case-sensitively: SQLite's LIKE is case-insensitive
        for ASCII, so '%EAR%' matches "Earth" and "research", and '%ITAR%'
        matches "military". GLOB is case-sensitive and avoids that entirely.
        """
        core = term.replace("-", "").replace(".", "")
        return len(core) <= 6 and core.isupper() and core.isalpha()

    def _query(self, category: str) -> List[Dict]:
        """
        Multi-strategy query returning sites relevant to a pipeline category.

        Precision rules:
          * Acronyms match case-sensitively via GLOB (see _is_acronym).
          * Word terms match on space-padded boundaries only -- no bare
            substring fallback, which previously defeated the padding.
          * Results are ordered before LIMIT so the same call returns the
            same rows on every machine and SQLite build.
          * Domains are used to *filter*, never to bulk-add every site they
            contain.
        """
        if category in self._cache:
            return self._cache[category]

        cfg     = _RESOURCE_MAP.get(category, {})
        results = {}

        allowed_domains = sorted(set(cfg.get("domains", [])) | {"Space & Aerospace"})
        domain_placeholders = ",".join("?" * len(allowed_domains))

        # Strategy 1: subdomain filter (most precise)
        for subdomain in cfg.get("subdomains", []):
            try:
                rows = self._wi._db._conn.execute(f"""
                    SELECT s.*, c.km_domain, c.km_subdomain, c.tags
                    FROM sites s
                    JOIN classifications c ON c.site_id = s.id
                    WHERE c.km_subdomain = ?
                      AND c.km_domain IN ({domain_placeholders})
                      AND s.is_private = 0
                    ORDER BY s.url
                """, [subdomain] + allowed_domains).fetchall()
                for r in rows:
                    results.setdefault(dict(r)["url"], dict(r))
            except Exception:
                pass

        # Strategy 2: term matching within allowed domains
        for term in cfg.get("terms", []):
            term = term.strip()
            if not term:
                continue
            if self._is_acronym(term):
                pats = [f"* {term} *", f"* {term}", f"{term} *", term]
                cond = " OR ".join(["s.description GLOB ?"] * len(pats)
                                   + ["s.name GLOB ?"] * len(pats))
                params = allowed_domains + pats + pats
            else:
                cond = ("(' ' || LOWER(s.description) || ' ') LIKE ? "
                        "OR (' ' || LOWER(s.name) || ' ') LIKE ?")
                pad = f"% {term.lower()} %"
                params = allowed_domains + [pad, pad]
            try:
                rows = self._wi._db._conn.execute(f"""
                    SELECT s.*, c.km_domain, c.km_subdomain, c.tags
                    FROM sites s
                    JOIN classifications c ON c.site_id = s.id
                    WHERE c.km_domain IN ({domain_placeholders})
                      AND ({cond})
                      AND s.is_private = 0
                    ORDER BY s.url
                    LIMIT 30
                """, params).fetchall()
                for r in rows:
                    results.setdefault(dict(r)["url"], dict(r))
            except Exception:
                pass

        # Strategy 3: FTS search, restricted to allowed domains
        for query in cfg.get("search", []):
            try:
                for site in self._wi.search(query, limit=30):
                    if site.get("km_domain", "") in allowed_domains:
                        results.setdefault(site["url"], site)
            except Exception:
                pass

        # Final filter: must be classified into an allowed domain.
        # Unclassified sites (km_domain == "") are no longer admitted - they
        # were a large share of the previous false-positive volume.
        out = [s for s in results.values()
               if s.get("km_domain", "") in allowed_domains]
        out.sort(key=lambda s: (s.get("name") or "").lower())

        # Collapse duplicate organisations: the same body often appears under
        # several URLs (www/non-www, /about, regional chapters). Keep the
        # shortest URL, which is almost always the canonical entry point.
        by_name = {}
        for s in out:
            key = " ".join((s.get("name") or s.get("url", "")).lower().split())
            prev = by_name.get(key)
            if prev is None or len(s.get("url", "")) < len(prev.get("url", "")):
                by_name[key] = s
        out = sorted(by_name.values(), key=lambda s: (s.get("name") or "").lower())
        self._cache[category] = out
        return out


# ══════════════════════════════════════════════════════════════════════════════
# Module-level convenience functions
# ══════════════════════════════════════════════════════════════════════════════

def tle_sources()        -> List[Dict]:
    with SatelliteResources() as r: return r.tle_sources()

def tracking_databases() -> List[Dict]:
    with SatelliteResources() as r: return r.tracking_databases()

def launch_providers()   -> List[Dict]:
    with SatelliteResources() as r: return r.launch_providers()

def regulatory_sources() -> List[Dict]:
    with SatelliteResources() as r: return r.regulatory_sources()

def space_agencies()     -> List[Dict]:
    with SatelliteResources() as r: return r.space_agencies()

def find(query: str, limit: int = 20) -> List[Dict]:
    with SatelliteResources() as r: return r.find(query, limit)


# ══════════════════════════════════════════════════════════════════════════════
# CLI — run directly to sync and report
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="WIT resource bridge for satellite platform")
    parser.add_argument("--sync",    action="store_true", help="Sync Space & Aerospace sites to satellite project")
    parser.add_argument("--summary", action="store_true", help="Show resource summary")
    parser.add_argument("--find",    type=str,            help="Search for resources")
    parser.add_argument("--tle",     action="store_true", help="List TLE sources")
    args = parser.parse_args()

    with SatelliteResources() as res:
        if args.sync:
            print("Syncing WIT satellite project...")
            res.sync(verbose=True)

        if args.summary or not any(vars(args).values()):
            s = res.summary()
            print("\n  WIT Satellite Resource Summary")
            print("  " + "─" * 36)
            for k, v in s.items():
                print(f"  {k:<25} {v:>6}")

        if args.find:
            results = res.find(args.find)
            print(f"\n  Results for '{args.find}': {len(results)}")
            for r in results[:10]:
                print(f"  • {r.get('name',''):<35} {r.get('url','')[:50]}")

        if args.tle:
            sources = res.tle_sources()
            print(f"\n  TLE Sources ({len(sources)}):")
            for s in sources:
                print(f"  • {s.get('name',''):<35} {s.get('url','')[:50]}")


def migrate_tags_from_classifications(verbose: bool = True) -> Dict[str, int]:
    """
    One-time migration: reads tags stored as JSON in classifications.tags
    and writes them to the site_tags table so tag-based queries work.
    Safe to run repeatedly (uses INSERT OR IGNORE).
    """
    import sys
    if str(_WIT_PATH) not in sys.path:
        sys.path.insert(0, str(_WIT_PATH))
    from wit import WebIntelligence
    import json as _json

    counts = {"sites": 0, "tags": 0, "skipped": 0}

    with WebIntelligence() as wi:
        rows = wi._db._conn.execute("""
            SELECT s.url, c.tags, c.km_domain, c.km_subdomain, c.content_type
            FROM sites s
            JOIN classifications c ON c.site_id = s.id
            WHERE c.tags IS NOT NULL AND c.tags != '[]' AND c.tags != ''
        """).fetchall()

        for row in rows:
            tags_raw = row["tags"]
            try:
                tags = _json.loads(tags_raw) if tags_raw else []
            except Exception:
                tags = [tags_raw] if tags_raw else []

            if not tags:
                counts["skipped"] += 1
                continue

            for tag in tags:
                tag = str(tag).strip()
                if not tag:
                    continue
                try:
                    wi._db.tag_site(row["url"], tag, "technology")
                    counts["tags"] += 1
                except Exception:
                    pass
            counts["sites"] += 1

        # Also tag by sector based on domain
        sector_map = {
            "Government & Policy":   "Government",
            "Defense & Intelligence":"Defense",
            "Research & Academic":   "Academic",
        }
        for km_domain, sector_tag in sector_map.items():
            sites = wi._db.get_sites(km_domain=km_domain)
            for s in sites:
                try:
                    wi._db.tag_site(s["url"], sector_tag, "sector")
                    counts["tags"] += 1
                except Exception:
                    pass

        # Tag use cases by subdomain
        use_map = {
            "Orbital Mechanics":         "Track Satellites",
            "Satellite Data & TLE":      "Track Satellites",
            "Satellite Data":            "Track Satellites",
            "Space Situational Awareness":"Track Satellites",
            "SBIR & Contracts":          "Find Funding",
            "Government Programs":       "Find Funding",
            "TRL & MRL Assessment":      "TRL/MRL Assessment",
            "Remote Sensing":            "Sensors & Payloads",
        }
        for subdomain, use_tag in use_map.items():
            sites = wi._db.get_sites(km_subdomain=subdomain)
            for s in sites:
                try:
                    wi._db.tag_site(s["url"], use_tag, "use")
                    counts["tags"] += 1
                except Exception:
                    pass

    if verbose:
        print(f"  Tag migration: {counts['sites']} sites, "
              f"{counts['tags']} tags applied, {counts['skipped']} skipped")
    return counts
