"""
Exportateur de snapshots statiques sécurisé par Publish Guard (Staging -> Validation -> Atomic Swap).
Empêche toute publication corrompue sur GitHub Pages / Vercel / Netlify si le Quality Gate est BLOCK.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

ENDPOINTS = [
    ("http://127.0.0.1:8080/api/listings", BASE_DIR / "data_listings.json"),
    ("http://127.0.0.1:8080/api/sources", BASE_DIR / "data_sources.json"),
    ("http://127.0.0.1:8080/api/agencies", BASE_DIR / "data_agencies.json"),
    ("http://127.0.0.1:8080/api/quality", BASE_DIR / "data_quality.json"),
]


def main(allow_block: bool = False) -> int:
    print("Exporting static snapshots with Production Quality Gate (staging -> atomic swap)...")
    staged: list[tuple[Path, Path, bytes]] = []

    for url, filepath in ENDPOINTS:
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
            # Validation JSON stricte avant staging
            parsed = json.loads(data.decode("utf-8"))
            if not isinstance(parsed, dict) or parsed.get("success") is False:
                raise ValueError(f"Payload invalide depuis {url}")
            staging_path = filepath.with_suffix(".staging.json")
            staging_path.write_bytes(data)
            staged.append((staging_path, filepath, data))
            print(f"  Staged {filepath.name} ({len(data):,} bytes)")
        except Exception as e:
            print(f"  [ERROR] Export failed for {url}: {e}")
            for sp, _, _ in staged:
                sp.unlink(missing_ok=True)
            return 1

    # Vérification bloquante Quality Gate sur data_quality.staging.json
    q_stage = BASE_DIR / "data_quality.staging.json"
    if q_stage.exists():
        q_payload = json.loads(q_stage.read_text(encoding="utf-8")).get("data") or {}
        gate_status = str(q_payload.get("gateStatus") or "PASS").upper()
        summary = q_payload.get("summary") or {}
        print(
            f"  [QUALITY GATE] Status={gate_status} | Publiées={summary.get('publishedCount')} | "
            f"Quarantaine={summary.get('quarantinedCount')} | Score moyen={summary.get('avgScore')}/100 | "
            f"Grade A={summary.get('gradeAPct')}% | Fusionnées={summary.get('fusionEnrichedCount')}"
        )
        if gate_status == "BLOCK" and not allow_block:
            print(f"  [BLOCKED] Publication bloquée par Quality Gate : {q_payload.get('blockReasons')}")
            for sp, _, _ in staged:
                sp.unlink(missing_ok=True)
            return 2

    # Swap atomique (os.replace) : aucun fichier corrompu / partiel en production
    for sp, fp, _ in staged:
        os.replace(sp, fp)
        print(f"  Published {fp.name} (atomic swap OK)")

    print("Static data snapshots validated and ready for production!")
    return 0


if __name__ == "__main__":
    sys.exit(main(allow_block="--force" in sys.argv))
