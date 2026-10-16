#!/usr/bin/env python3
# Confronto Meteo - Raccoglitore V1
#
# Cosa fa, per ciascuno dei 4 modelli italiani elencati in modelli.json:
#  1. Scarica l'ultimo pacchetto pubblico da MeteoHub (nessun account richiesto).
#  2. Legge il file GRIB2 ed estrae, per ciascuna delle 12 localita' in localita.json,
#     tutte le ore di previsione disponibili per temperatura, vento, nuvolosita' e pioggia.
#  3. Scrive il risultato su Firestore (progetto Firebase dell'app), un documento per
#     localita', con dentro i dati di tutti i modelli.
#  4. Cancella il file GRIB2 scaricato.
#
# Richiede la variabile d'ambiente FIREBASE_SERVICE_ACCOUNT con il contenuto JSON
# della chiave del service account (impostata come GitHub Secret, mai nel codice).
#
# Uso pensato: dentro GitHub Actions (vedi .github/workflows/raccoglitore.yml), mai
# sul PC: serve la libreria di sistema eccodes, difficile da installare su Windows.

import json
import math
import os
import sys
import tempfile
import time
import urllib.request

VERSIONE = "V1"
BASE = "https://meteohub.agenziaitaliameteo.it"
QUI = os.path.dirname(os.path.abspath(__file__))
FIRESTORE_COLLEZIONE = "meteo_italia"

with open(os.path.join(QUI, "localita.json"), encoding="utf-8") as fh:
    LOCALITA = json.load(fh)
with open(os.path.join(QUI, "modelli.json"), encoding="utf-8") as fh:
    MODELLI = json.load(fh)

# variabili che leggiamo da ogni file GRIB2 (nomi brevi usati da eccodes)
VAR_TEMPERATURA = "2t"
VAR_VENTO_U = "10u"
VAR_VENTO_V = "10v"
VAR_NUVOLE = "clct"
VAR_PIOGGIA = "tp"  # cumulata dall'inizio della corsa
TUTTE_LE_VARIABILI = [VAR_TEMPERATURA, VAR_VENTO_U, VAR_VENTO_V, VAR_NUVOLE, VAR_PIOGGIA]


def passo_numero(valore):
    """Converte endStep in un numero intero, qualunque formato restituisca eccodes."""
    try:
        return int(valore)
    except (TypeError, ValueError):
        pass
    testo = str(valore)
    pezzo = testo.split("-")[-1].strip()
    try:
        return int(pezzo)
    except ValueError:
        return 0


def scarica_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def scarica_file(url, percorso):
    print(f"    scarico {url}")
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=900) as r, open(percorso, "wb") as out:
        totale = 0
        while True:
            blocco = r.read(1024 * 1024)
            if not blocco:
                break
            out.write(blocco)
            totale += len(blocco)
    print(f"    fatto: {totale / 1048576:.1f} MB in {time.time() - t0:.0f} s")
    return totale


def trova_ultimo_pacchetto(dataset):
    url = f"{BASE}/api/datasets/{dataset}/opendata"
    lista = scarica_json(url)
    if not lista:
        raise RuntimeError(f"nessun pacchetto disponibile per {dataset}")
    lista.sort(key=lambda u: (u.get("date", ""), u.get("run", "")), reverse=True)
    return lista[0]


def estrai_variabile(eccodes, percorso_grib, variabile, localita):
    """Legge una sola volta tutto il file cercando solo 'variabile' e restituisce,
    per ogni localita' (per id), un dizionario {passo_ore: valore}."""
    risultato = {loc["id"]: {} for loc in localita}
    with open(percorso_grib, "rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                short = eccodes.codes_get(gid, "shortName")
                if short != variabile:
                    continue
                step = passo_numero(eccodes.codes_get(gid, "endStep"))
                for loc in localita:
                    vicini = eccodes.codes_grib_find_nearest(gid, loc["lat"], loc["lon"])
                    punto = vicini[0] if isinstance(vicini, (list, tuple)) else vicini
                    risultato[loc["id"]][step] = punto.value
            finally:
                eccodes.codes_release(gid)
    return risultato


def elabora_modello(eccodes, modello):
    print(f"\n== {modello['nome']} ({modello['dataset']})")
    pacchetto = trova_ultimo_pacchetto(modello["dataset"])
    nome_file = pacchetto.get("filename")
    corsa = f"{pacchetto.get('date')}T{str(pacchetto.get('run', '00')).zfill(2)}:00:00Z"
    print(f"  pacchetto: {nome_file}  (corsa {corsa})")
    if not nome_file:
        print("  ERRORE: pacchetto senza nome file, salto questo modello")
        return None

    percorso_grib = os.path.join(QUI, nome_file)
    scarica_file(f"{BASE}/api/opendata/{nome_file}", percorso_grib)

    try:
        print("  leggo le variabili dal file GRIB2...")
        per_variabile = {}
        for var in TUTTE_LE_VARIABILI:
            per_variabile[var] = estrai_variabile(eccodes, percorso_grib, var, LOCALITA)
            n_passi = len(next(iter(per_variabile[var].values()), {}))
            print(f"    {var}: {n_passi} istanti letti")
    finally:
        try:
            os.remove(percorso_grib)
        except OSError:
            pass

    # ricompongo i dati per localita'
    risultati_per_localita = {}
    for loc in LOCALITA:
        lid = loc["id"]
        passi_temp = sorted(per_variabile[VAR_TEMPERATURA].get(lid, {}).keys())
        pioggia_cum_precedente = None
        ore = []
        for step in passi_temp:
            t_kelvin = per_variabile[VAR_TEMPERATURA][lid].get(step)
            u = per_variabile[VAR_VENTO_U].get(lid, {}).get(step)
            v = per_variabile[VAR_VENTO_V].get(lid, {}).get(step)
            nuvole = per_variabile[VAR_NUVOLE].get(lid, {}).get(step)
            pioggia_cum = per_variabile[VAR_PIOGGIA].get(lid, {}).get(step)

            vento = round(math.sqrt(u * u + v * v), 1) if u is not None and v is not None else None
            if pioggia_cum is not None:
                base = pioggia_cum_precedente if pioggia_cum_precedente is not None else 0.0
                pioggia_ora = round(max(0.0, pioggia_cum - base), 2)
                pioggia_cum_precedente = pioggia_cum
            else:
                pioggia_ora = None

            ore.append({
                "h": step,
                "t": round(t_kelvin - 273.15, 1) if t_kelvin is not None else None,
                "vento": vento,
                "nuvole": round(nuvole) if nuvole is not None else None,
                "pioggia": pioggia_ora,
            })
        risultati_per_localita[lid] = {"corsa": corsa, "generato": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ore": ore}
    return risultati_per_localita


def scrivi_su_firestore(dati_per_modello):
    chiave_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not chiave_json:
        print("\n(FIREBASE_SERVICE_ACCOUNT non impostata: salto la scrittura su Firestore, stampo solo un riepilogo)")
        for mid, per_loc in dati_per_modello.items():
            for lid, dati in per_loc.items():
                print(f"  {mid} / {lid}: {len(dati['ore'])} ore, corsa {dati['corsa']}")
        return

    import firebase_admin
    from firebase_admin import credentials, firestore

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        tf.write(chiave_json)
        percorso_chiave = tf.name

    try:
        cred = credentials.Certificate(percorso_chiave)
        firebase_admin.initialize_app(cred)
        db = firestore.client()

        print("\n== Scrivo su Firestore")
        for loc in LOCALITA:
            lid = loc["id"]
            modelli_loc = {}
            for mid, per_loc in dati_per_modello.items():
                if lid in per_loc:
                    modelli_loc[mid] = per_loc[lid]
            if not modelli_loc:
                continue
            doc = db.collection(FIRESTORE_COLLEZIONE).document(lid)
            doc.set({
                "nome": loc["nome"],
                "lat": loc["lat"],
                "lon": loc["lon"],
                "aggiornato": firestore.SERVER_TIMESTAMP,
                "modelli": modelli_loc,
            }, merge=True)
            print(f"  scritto: {lid} ({', '.join(modelli_loc.keys())})")
    finally:
        try:
            os.remove(percorso_chiave)
        except OSError:
            pass


def main():
    print(f"Raccoglitore {VERSIONE} - {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"Localita': {len(LOCALITA)}  |  Modelli: {len(MODELLI)}")

    try:
        import eccodes
    except ImportError:
        print("ERRORE: manca la libreria 'eccodes' (vedi requirements.txt / workflow GitHub Actions)")
        sys.exit(1)

    dati_per_modello = {}
    for modello in MODELLI:
        try:
            risultato = elabora_modello(eccodes, modello)
            if risultato:
                dati_per_modello[modello["id"]] = risultato
        except Exception as e:
            print(f"  ERRORE su {modello['nome']}: {e}")

    scrivi_su_firestore(dati_per_modello)
    print("\nFatto.")


if __name__ == "__main__":
    main()
