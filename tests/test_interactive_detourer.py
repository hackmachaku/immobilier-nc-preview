"""
Test interactif de validation du bouton 'Détourer Lot' et 'Voir & Détourer sur la Carte'
via Chrome DevTools Protocol (CDP).
"""
import asyncio
import json
import subprocess
import time
import urllib.request
import websockets
import pytest


async def run_cdp_test():
    import tempfile
    user_data = tempfile.mkdtemp(prefix="chrome_cdp_")
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    port = 9224
    proc = subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--window-size=1400,900",
        "http://localhost:8080/dashboard"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # Attendre que Chrome CDP réponde
        ws_url = None
        for _ in range(25):
            time.sleep(0.3)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as resp:
                    tabs = json.loads(resp.read().decode())
                    for t in tabs:
                        if t.get("type") == "page" and "webSocketDebuggerUrl" in t:
                            ws_url = t["webSocketDebuggerUrl"]
                            break
                    if ws_url:
                        break
            except Exception:
                pass

        assert ws_url is not None, "Impossible de se connecter au port CDP de Chrome"

        async with websockets.connect(ws_url) as ws:
            msg_id = 0

            async def send_cmd(method, params=None):
                nonlocal msg_id
                msg_id += 1
                cmd = {"id": msg_id, "method": method, "params": params or {}}
                await ws.send(json.dumps(cmd))
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if data.get("id") == msg_id:
                        return data.get("result", {})

            # 1. Naviguer vers le dashboard
            await send_cmd("Page.enable")
            await send_cmd("Runtime.enable")
            await send_cmd("Page.navigate", {"url": "http://localhost:8080/dashboard"})
            
            for _ in range(20):
                await asyncio.sleep(0.3)
                res = await send_cmd("Runtime.evaluate", {"expression": "document.readyState"})
                if res.get("result", {}).get("value") == "complete":
                    break

            await asyncio.sleep(1.0)

            # 2. Vérifier que la page s'est chargée
            res_title = await send_cmd("Runtime.evaluate", {"expression": "document.title"})
            page_title = res_title.get("result", {}).get("value", "")
            print(f"DEBUG: page_title = {page_title}")

            # 3. Basculer sur l'onglet Carte
            res_switch = await send_cmd("Runtime.evaluate", {
                "expression": "switchTab('carte'); (typeof leafletMapInstance !== 'undefined' && leafletMapInstance !== null)"
            })
            await asyncio.sleep(1.5)

            # 4. Vérifier que leafletMapInstance est bien initialisé
            res_map = await send_cmd("Runtime.evaluate", {
                "expression": "(typeof leafletMapInstance !== 'undefined' && leafletMapInstance !== null)"
            })
            assert res_map.get("result", {}).get("value") is True, "Leaflet Map Instance non initialisée"

            # 5. Tester l'appel direct à showCadastreParcelPolygon
            # Coordonnées Nouméa Anse Vata
            test_lat = -22.2984
            test_lon = 166.4399
            await send_cmd("Runtime.evaluate", {
                "expression": f"showCadastreParcelPolygon({test_lat}, {test_lon}, true)"
            })

            # Attendre que la requête réseau ArcGIS / API locale se termine
            await asyncio.sleep(2.5)

            # 6. Vérifier que currentParcelPolygonLayer est présent et valide sur la carte
            res_polygon = await send_cmd("Runtime.evaluate", {
                "expression": """
                (() => {
                    if (!currentParcelPolygonLayer) return { exists: false };
                    const hasLayers = Object.keys(currentParcelPolygonLayer._layers || {}).length > 0;
                    const bounds = currentParcelPolygonLayer.getBounds();
                    const isValid = bounds && bounds.isValid();
                    return {
                        exists: true,
                        hasLayers: hasLayers,
                        isValid: isValid
                    };
                })()
                """,
                "returnByValue": True
            })
            poly_data = res_polygon.get("result", {}).get("value", {})
            assert poly_data.get("exists") is True, f"currentParcelPolygonLayer n'existe pas : {poly_data}"
            assert poly_data.get("hasLayers") is True, "currentParcelPolygonLayer n'a pas de couches vectorielles"
            assert poly_data.get("isValid") is True, "Les coordonnées du polygone sont invalides"

            # 7. Vérifier la présence du toast de notification
            res_toast = await send_cmd("Runtime.evaluate", {
                "expression": """
                (() => {
                    const toast = document.getElementById('toastNotificationContainer');
                    return toast ? toast.innerText : '';
                })()
                """
            })
            toast_text = res_toast.get("result", {}).get("value", "")
            assert "Parcelle Délimitée" in toast_text or "Cadastre" in toast_text, f"Toast inattendu: {toast_text}"

            # 8. Tester le bouton 'viewOnMapAndTrace' depuis la modale
            res_modal_trace = await send_cmd("Runtime.evaluate", {
                "expression": f"viewOnMapAndTrace(-22.2008, 166.4474)"
            })
            await asyncio.sleep(2.5)

            res_polygon2 = await send_cmd("Runtime.evaluate", {
                "expression": """
                (() => {
                    if (!currentParcelPolygonLayer) return false;
                    return Object.keys(currentParcelPolygonLayer._layers || {}).length > 0;
                })()
                """
            })
            assert res_polygon2.get("result", {}).get("value") is True, "viewOnMapAndTrace n'a pas tracé la parcelle"

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def test_cadastre_detourer_interactive():
    """Exécute la boucle async de test CDP."""
    asyncio.run(run_cdp_test())

if __name__ == '__main__':
    test_cadastre_detourer_interactive()
    print("Test CDP Cadastre Détourer réussi avec succès !")
