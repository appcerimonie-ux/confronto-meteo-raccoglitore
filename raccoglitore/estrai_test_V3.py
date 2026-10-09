#!/usr/bin/env python3
# Confronto Meteo - Raccoglitore - Test di estrazione V3
# (V2: provato a usare "endStep" invece di "step" per il numero di ore di previsione.
#  V3: anche "endStep" puo' arrivare come testo invece che numero (dipende dal tipo di
#  campo dentro il GRIB2); ora il valore viene convertito a numero esplicitamente,
#  qualunque cosa restituisca eccodes)
#
# Cosa fa:
#  1. Scarica dall'opendata pubblico di MeteoHub (nessun account richiesto)
#     l'ultimo pacchetto disponibile di un modello italiano (default: WRF_DA_ITA,
#     il piu' leggero, ~60 MB).
#  2. Legge il file GRIB2 con la libreria eccodes e stampa un riepilogo di cosa
#     contiene (quali variabili, quanti istanti di previsione).
#  3. Per la prima variabile di temperatura o pioggia trovata, estrae il valore
#     nel punto di griglia piu' vicino a ciascuna delle 12 localita' seguite.
#  4. Scrive i risultati in estrazione_test.json, nella stessa cartella.
#
# NON scrive su Firestore, NON cancella nulla su MeteoHub: e' solo una prova
# tecnica per verificare che scaricare + leggere il GRIB2 funzioni, prima di
# costruire il raccoglitore vero (che girera' ogni 3 ore via GitHub Actions).
#
# Uso in locale (richiede la libreria di sistema eccodes, difficile su Windows):
#   pip install -r requirements.txt
#   python estrai_test_V3.py
#
# Uso pensato (consigliato): dentro GitHub Actions, dove la libreria di sistema
# si installa con un comando apt (vedi .github/workflows/test-estrazione.yml).
# Non serve installare nulla sul PC per questo test.

import json
import os
import sys
import time
import urllib.request

VERSIONE = "V3"
BASE = "https://meteohub.agenziaitaliameteo.it"
QUI = os.path.dirname(os.path.abspath(__file__))

# dataset da provare, in ordine: il primo che risponde con un pacchetto valido viene usato.
# WRF_DA_ITA (WRF con assimilazione radar) e' il piu' leggero (~60 MB), buono per un primo test.
DATASET_DI_PROVA = sys.argv[1] if len(sys.argv) > 1 else "WRF_DA_ITA"

with open(os.path.join(QUI, "localita.json"), encoding="utf-8") as fh:
    LOCALITA = json.load(fh)

# variabili che ci interessano per il confronto meteo (temperatura 2m, pioggia, nuvolosita', vento)
VARIABILI_UTILI = ["2t", "t2m", "tp", "tprate", "prate", "tcc", "10si", "10u", "10v"]


def passo_numero(valore):
    """Converte il passo di previsione (endStep) in un numero intero, qualunque sia
    il formato restituito da eccodes (a volte un numero, a volte un testo tipo "0-3")."""
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
    print(f"  scarico {url}")
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=600) as r, open(percorso, "wb") as out:
        totale = 0
        while True:
            blocco = r.read(1024 * 1024)
            if not blocco:
                break
            out.write(blocco)
            totale += len(blocco)
    print(f"  fatto: {totale / 1048576:.1f} MB in {time.time() - t0:.0f} s")
    return totale


def trova_ultimo_pacchetto(dataset):
    url = f"{BASE}/api/datasets/{dataset}/opendata"
    lista = scarica_json(url)
    if not lista:
        raise RuntimeError(f"nessun pacchetto disponibile per {dataset}")
    lista.sort(key=lambda u: (u.get("date", ""), u.get("run", "")), reverse=True)
    return lista[0]


def main():
    print(f"Raccoglitore - test estrazione {VERSIONE}")
    print(f"Localita' caricate: {len(LOCALITA)}")
    print(f"Dataset di prova: {DATASET_DI_PROVA}\n")

    print("== Passo 1: cerco l'ultimo pacchetto disponibile")
    pacchetto = trova_ultimo_pacchetto(DATASET_DI_PROVA)
    nome_file = pacchetto.get("filename")
    print(f"  trovato: {nome_file}  (corsa {pacchetto.get('date')} {pacchetto.get('run', '')})")
    if not nome_file:
        print("  ERRORE: il pacchetto non ha un nome file (filename). Risposta completa:")
        print(json.dumps(pacchetto, indent=2)[:1000])
        sys.exit(1)

    print("\n== Passo 2: scarico il file")
    percorso_grib = os.path.join(QUI, nome_file)
    url_download = f"{BASE}/api/opendata/{nome_file}"
    scarica_file(url_download, percorso_grib)

    print("\n== Passo 3: leggo il file GRIB2 con eccodes")
    try:
        import eccodes
    except ImportError:
        print("  ERRORE: manca la libreria 'eccodes'. Installa con: pip install -r requirements.txt")
        print("  Su Windows questa libreria e' difficile da installare: questo script va fatto girare")
        print("  dentro GitHub Actions (vedi .github/workflows/test-estrazione.yml), non sul PC.")
        sys.exit(1)

    riepilogo = {}
    messaggi_utili = []
    with open(percorso_grib, "rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                short = eccodes.codes_get(gid, "shortName")
                step = passo_numero(eccodes.codes_get(gid, "endStep"))
                riepilogo.setdefault(short, set()).add(step)
                if short in VARIABILI_UTILI:
                    messaggi_utili.append((short, step, gid))
                    continue  # non rilascio: lo useremo sotto
            except Exception as e:
                print(f"  (messaggio illeggibile: {e})")
            eccodes.codes_release(gid)

    print("  variabili trovate nel file:")
    for nome, step in sorted(riepilogo.items()):
        passi = sorted(step)
        print(f"    {nome}: {len(passi)} istanti di previsione (es. +{passi[0]}h ... +{passi[-1]}h)")

    if not messaggi_utili:
        print("\n  ATTENZIONE: nessuna delle variabili attese (temperatura/pioggia) e' stata trovata.")
        print("  Variabili utili cercate:", VARIABILI_UTILI)
        sys.exit(0)

    # prendo solo il primo passo temporale disponibile per ciascuna variabile utile, per il test
    primo_passo = {}
    for short, step, gid in messaggi_utili:
        if short not in primo_passo or step < primo_passo[short][0]:
            if short in primo_passo:
                eccodes.codes_release(primo_passo[short][1])
            primo_passo[short] = (step, gid)
        else:
            eccodes.codes_release(gid)

    print(f"\n== Passo 4: estraggo i valori per le {len(LOCALITA)} localita' seguite")
    risultati = []
    for loc in LOCALITA:
        riga = {"id": loc["id"], "nome": loc["nome"]}
        for short, (step, gid) in primo_passo.items():
            try:
                vicini = eccodes.codes_grib_find_nearest(gid, loc["lat"], loc["lon"])
                punto = vicini[0] if isinstance(vicini, (list, tuple)) else vicini
                riga[short] = {
                    "valore": punto.value,
                    "distanza_km": round(punto.distance, 2),
                    "passo_ore": step,
                }
            except Exception as e:
                riga[short] = {"errore": str(e)}
        risultati.append(riga)
        pezzi = ", ".join(
            f"{k}={v.get('valore', v.get('errore'))}" for k, v in riga.items() if k not in ("id", "nome")
        )
        print(f"  {loc['nome']}: {pezzi}")

    for step, gid in primo_passo.values():
        eccodes.codes_release(gid)

    out_path = os.path.join(QUI, "estrazione_test.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"dataset": DATASET_DI_PROVA, "pacchetto": nome_file, "risultati": risultati},
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\nFatto. Risultati scritti in {out_path}")

    try:
        os.remove(percorso_grib)
        print(f"File GRIB2 ({nome_file}) cancellato per non riempire lo spazio disco.")
    except OSError as e:
        print(f"(non sono riuscito a cancellare il file GRIB2: {e})")


if __name__ == "__main__":
    main()
