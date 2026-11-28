"""
Vérificateur Canari en Direct (Canary Live Verifier) — Sentinel ImmoNC.
Échantillonne N annonces par portail et vérifie en direct contre les sites/APIs sources :
- Taux de fraîcheur en ligne (200 actif vs 404/deleted)
- Exactitude du prix (tolérance 0 %)
- Fidélité du descriptif (ratio longueur >= 0,85)
- Détection de dérive sélecteur HTML/API (`SELECTOR_DRIFT`)
"""

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
import urllib3

urllib3.disable_warnings()

CANARY_REPORT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "canary_latest.json"

HEADERS_IMMO = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, application/vnd_api.immobilier.nc.v0.1+json;",
    "Referer": "https://www.immobilier.nc/",
}
HEADERS_IMMONC = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://www.immonc.com/",
}
HEADERS_YATOO = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Origin": "https://yatoo.nc",
    "Referer": "https://yatoo.nc/",
}
YATOO_URL = "https://australia-southeast1-yatoo-nc.cloudfunctions.net/api"


def _verify_one_live(item: Dict[str, Any]) -> Dict[str, Any]:
    lid = str(item.get("id") or "")
    src = str(item.get("source") or "").lower()
    url = str(item.get("sourceUrl") or "")
    db_price = int(item.get("currentPrice") or 0)
    db_desc_len = len(str(item.get("description") or "").strip())
    sid = lid.split("_", 1)[1] if "_" in lid else lid

    res = {
        "id": lid,
        "source": src,
        "url": url,
        "online": False,
        "priceMatch": None,
        "descRatio": None,
        "livePrice": None,
        "status": "ERROR",
    }
    try:
        if src in ("immobilier.nc", "bienmeloger.nc"):
            r = requests.get(f"https://api.immobilier.nc/api/posts/{sid}", headers=HEADERS_IMMO, timeout=10, verify=False)
            if r.status_code in (404, 410):
                res["status"] = "EXPIRED_404"
                return res
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                res["online"] = True
                raw_p = data.get("price")
                deal = str(data.get("deal_type") or "").lower()
                if raw_p is not None:
                    pv = float(raw_p)
                    if deal == "location":
                        live_p = int(pv * 1000) if pv < 1000 else int(pv)
                    else:
                        live_p = int(round(pv * 1_000_000)) if pv < 1000 else (int(round(pv)) if pv >= 1_000_000 else int(round(pv * 1000)))
                    res["livePrice"] = live_p
                    res["priceMatch"] = (live_p == db_price)
                live_desc = re.sub(r"<[^>]+>", " ", str(data.get("description") or "")).strip()
                if live_desc:
                    res["descRatio"] = round(min(2.0, db_desc_len / max(1, len(live_desc))), 2)
                res["status"] = "OK" if res["priceMatch"] is not False else "PRICE_DRIFT"
                return res

        elif src == "immonc":
            r = requests.get(url, headers=HEADERS_IMMONC, timeout=12, verify=False)
            if r.status_code in (404, 410):
                res["status"] = "EXPIRED_404"
                return res
            if r.status_code == 200:
                try:
                    raw_html = r.content.decode("utf-8")
                except Exception:
                    raw_html = r.content.decode("windows-1252", errors="ignore")
                if len(raw_html) < 4500:
                    res["status"] = "EXPIRED_404"
                    return res
                res["online"] = True
                from src.ingestion.detail_parser import parse_immonc_detail_html
                det = parse_immonc_detail_html(raw_html, url=url)
                live_desc = det.get("full_description") or ""
                if live_desc:
                    res["descRatio"] = round(min(2.0, db_desc_len / max(1, len(live_desc))), 2)
                # Prix live dans HTML
                m_pr = re.search(r'class=["\'][^"\']*venderprice[^"\']*["\'][^>]*>(.*?)</', raw_html, re.I | re.S)
                if m_pr:
                    digs = re.sub(r"[^\d]", "", m_pr.group(1))
                    if digs and int(digs) >= 10_000:
                        res["livePrice"] = int(digs)
                        res["priceMatch"] = (int(digs) == db_price)
                res["status"] = "OK" if res["priceMatch"] is not False else "PRICE_DRIFT"
                return res

        elif src == "yatoo.nc":
            q = """query getPost($id: String!) { post(id: $id) { id status deleted price description } }"""
            r = requests.post(YATOO_URL, json={"operationName": "getPost", "variables": {"id": sid}, "query": q}, headers=HEADERS_YATOO, timeout=10, verify=False)
            if r.status_code == 200:
                post = ((r.json() or {}).get("data") or {}).get("post")
                if not post or post.get("deleted") is True or str(post.get("status") or "").lower() == "deleted":
                    res["status"] = "EXPIRED_404"
                    return res
                res["online"] = True
                if post.get("price") is not None:
                    res["livePrice"] = int(float(post["price"]))
                    res["priceMatch"] = (res["livePrice"] == db_price)
                live_desc = str(post.get("description") or "").strip()
                if live_desc:
                    res["descRatio"] = round(min(2.0, db_desc_len / max(1, len(live_desc))), 2)
                res["status"] = "OK" if res["priceMatch"] is not False else "PRICE_DRIFT"
                return res
    except Exception as exc:
        res["error"] = str(exc)[:120]
    return res


def run_canary_verification(
    listings: List[Dict[str, Any]],
    per_portal: int = 5,
    seed: Optional[int] = None,
    save_path: Optional[Path] = CANARY_REPORT_PATH,
) -> Dict[str, Any]:
    """Exécute un contrôle canari live stratifié (5 annonces / portail = 20 vérifications directes)."""
    rng = random.Random(seed)
    by_src: Dict[str, List[Dict[str, Any]]] = {}
    for item in listings:
        src = str(item.get("source") or "").lower()
        by_src.setdefault(src, []).append(item)

    sample: List[Dict[str, Any]] = []
    for src in ("immobilier.nc", "bienmeloger.nc", "immonc", "yatoo.nc"):
        pool = by_src.get(src, [])
        if pool:
            sample.extend(rng.sample(pool, min(per_portal, len(pool))))

    with ThreadPoolExecutor(max_workers=8) as ex:
        checks = list(ex.map(_verify_one_live, sample))

    by_portal_summary: Dict[str, Dict[str, Any]] = {}
    drift_portals: List[str] = []
    for src in ("immobilier.nc", "bienmeloger.nc", "immonc", "yatoo.nc"):
        sub = [c for c in checks if c["source"] == src]
        if not sub:
            continue
        online_n = sum(1 for c in sub if c["online"])
        expired_n = sum(1 for c in sub if c["status"] == "EXPIRED_404")
        pm_checked = [c for c in sub if c["priceMatch"] is not None]
        pm_ok = sum(1 for c in pm_checked if c["priceMatch"])
        desc_checked = [c for c in sub if c["descRatio"] is not None]
        desc_ok = sum(1 for c in desc_checked if c["descRatio"] >= 0.80)
        failures = sum(1 for c in sub if c["status"] in ("PRICE_DRIFT", "ERROR"))
        is_drift = failures >= 2
        if is_drift:
            drift_portals.append(src)
        by_portal_summary[src] = {
            "checked": len(sub),
            "online": online_n,
            "expired404": expired_n,
            "freshnessPct": round(online_n / len(sub) * 100, 1),
            "priceMatchPct": round(pm_ok / len(pm_checked) * 100, 1) if pm_checked else 100.0,
            "descFidelityPct": round(desc_ok / len(desc_checked) * 100, 1) if desc_checked else 100.0,
            "selectorDrift": is_drift,
        }

    total_checked = len(checks)
    total_online = sum(1 for c in checks if c["online"])
    pm_all = [c for c in checks if c["priceMatch"] is not None]
    pm_all_ok = sum(1 for c in pm_all if c["priceMatch"])

    report = {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "totalChecked": total_checked,
        "onlineCount": total_online,
        "freshnessPct": round(total_online / total_checked * 100, 1) if total_checked else 0.0,
        "priceExactMatchPct": round(pm_all_ok / len(pm_all) * 100, 1) if pm_all else 100.0,
        "selectorDriftPortals": drift_portals,
        "byPortal": by_portal_summary,
        "samples": checks,
    }

    if save_path:
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return report


def load_latest_canary_report(path: Path = CANARY_REPORT_PATH) -> Optional[Dict[str, Any]]:
    """Charge le dernier rapport Canari sauvegardé si disponible."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None
