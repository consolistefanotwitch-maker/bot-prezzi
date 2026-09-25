#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_ebay.py — Sorveglia i tuoi film su eBay (tutti i mercati) e ti avvisa
su Telegram quando compaiono offerte compatibili: regione B o free, con la
regola sui sottotitoli che hai scelto.

Affianca Keepa: Keepa copre Amazon, questo copre eBay (usato e import), e
gli avvisi arrivano nello stesso Telegram.

REGOLE DI COMPATIBILITÀ (le tue)
--------------------------------
- Solo dischi Region B o Region Free (gli altri vengono scartati).
- Live action: vanno bene sottotitoli in italiano O in inglese.
- Animazione: serve almeno il sottotitolo in italiano (o audio ita).
- I sottotitoli su eBay spesso non sono dichiarati: quando mancano,
  l'offerta arriva comunque ma marcata "sottotitoli da verificare".

--------------------------------------------------------------------
COSA SERVE (gratis)
--------------------------------------------------------------------
1) Chiavi eBay (API ufficiale, gratuita):
   - developer.ebay.com -> registrati -> crea un'app -> chiavi "Production"
   - servono EBAY_CLIENT_ID e EBAY_CLIENT_SECRET
2) Bot Telegram (come per Keepa):
   - @BotFather -> /newbot -> TELEGRAM_TOKEN
   - scrivi al bot, poi apri
     https://api.telegram.org/bot<TOKEN>/getUpdates -> TELEGRAM_CHAT_ID
3) La tua lista esportata: lista_desideri.json accanto a questo file.

IMPOSTAZIONI (variabili d'ambiente o file chiavi.env)
    EBAY_CLIENT_ID=...
    EBAY_CLIENT_SECRET=...
    TELEGRAM_TOKEN=...
    TELEGRAM_CHAT_ID=...
    ORE_TRA_I_GIRI=6
    MERCATI=IT,DE,FR,GB,US        # mercati eBay da interrogare
    SOLO_UNA_VOLTA=0             # 1 = un giro solo e poi esce (per prova)
    SOLO=                        # filtra i titoli che contengono questo testo
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Manca requests. Installa con:  pip install requests")

CARTELLA = Path(__file__).resolve().parent
FILE_CHIAVI = CARTELLA / "chiavi.env"
FILE_LISTA = CARTELLA / "lista_desideri.json"
FILE_VISTI = CARTELLA / "ebay_gia_visti.json"


# ----------------------------------------------------------------------
# Impostazioni
# ----------------------------------------------------------------------

def carica_chiavi() -> None:
    if FILE_CHIAVI.exists():
        for riga in FILE_CHIAVI.read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if riga and not riga.startswith("#") and "=" in riga:
                k, v = riga.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


carica_chiavi()

EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ORE_TRA_I_GIRI = float(os.environ.get("ORE_TRA_I_GIRI", "6"))
SOLO_UNA_VOLTA = os.environ.get("SOLO_UNA_VOLTA", "0") == "1"
SOLO = os.environ.get("SOLO", "").strip().lower()

# mercato eBay -> (marketplace id, valuta, sigla leggibile)
MERCATI_DISPONIBILI = {
    "IT": ("EBAY_IT", "EUR", "🇮🇹"),
    "DE": ("EBAY_DE", "EUR", "🇩🇪"),
    "FR": ("EBAY_FR", "EUR", "🇫🇷"),
    "ES": ("EBAY_ES", "EUR", "🇪🇸"),
    "GB": ("EBAY_GB", "GBP", "🇬🇧"),
    "US": ("EBAY_ENUS", "USD", "🇺🇸"),
}
MERCATI = [m.strip().upper() for m in os.environ.get("MERCATI", "IT,DE,FR,GB,US").split(",")
           if m.strip().upper() in MERCATI_DISPONIBILI]

CAMBI = {"EUR": 1.0, "GBP": 1.17, "USD": 0.92, "JPY": 0.0061}

MAX_PER_MERCATO = 8  # quante offerte esaminare per film per mercato

# parole che indicano roba da scartare
RUMORE = re.compile(
    r"\b(custodia|case only|solo custodia|copertina|poster|locandina|"
    r"soundtrack|colonna sonora|libro|book|t-shirt|maglietta|figure|funko|"
    r"vuoto|empty|replacement|sostitutiv|promo\b|screener)\w*\b", re.IGNORECASE)

# indizi di regione nel testo
REG_B = re.compile(r"\b(region\s*[- ]?\s*b|regione\s*b|region\s*free|regione\s*libera|reg\.?\s*b|multiregion|multi[- ]?region|region\s*abc|zone\s*b)\b", re.IGNORECASE)
REG_ALTRE = re.compile(r"\b(region\s*[- ]?\s*a\b|region\s*[- ]?\s*c\b|regione\s*a\b|regione\s*c\b|reg\.?\s*a\b|reg\.?\s*c\b|zone\s*a\b|ntsc\s*region\s*1)\b", re.IGNORECASE)

# indizi sottotitoli
SUB_IT = re.compile(r"\b(sub\s*ita|sottotitoli\s*italian|italian\s*sub|subtitles?\s*[^.]*italian|ita\s*sub|audio\s*ita|italiano)\b", re.IGNORECASE)
SUB_EN = re.compile(r"\b(sub\s*eng|english\s*sub|subtitles?\s*[^.]*english|eng\s*sub|audio\s*eng|english)\b", re.IGNORECASE)


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------

def telegram(testo: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("[Telegram non configurato]\n" + testo + "\n")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": testo,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=20)
        if r.status_code != 200:
            print("Telegram:", r.status_code, r.text[:150])
    except Exception as e:
        print("Errore Telegram:", e)


# ----------------------------------------------------------------------
# eBay
# ----------------------------------------------------------------------

class EBay:
    TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    def __init__(self) -> None:
        self._token = None
        self._scad = 0.0

    @property
    def attiva(self) -> bool:
        return bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)

    def token(self) -> str:
        if self._token and time.time() < self._scad - 60:
            return self._token
        basic = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
        r = requests.post(self.TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=20)
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._scad = time.time() + int(d.get("expires_in", 7200))
        return self._token

    def cerca(self, query: str, mercato: str) -> list[dict]:
        mk, valuta, _ = MERCATI_DISPONIBILI[mercato]
        try:
            r = requests.get(self.SEARCH_URL,
                headers={"Authorization": f"Bearer {self.token()}",
                         "X-EBAY-C-MARKETPLACE-ID": mk, "Accept": "application/json"},
                params={"q": query, "limit": MAX_PER_MERCATO, "sort": "price",
                        "filter": "buyingOptions:{FIXED_PRICE|AUCTION}"},
                timeout=25)
        except Exception as e:
            print(f"   eBay {mercato}: {e}")
            return []
        if r.status_code == 429:
            print(f"   eBay {mercato}: troppe richieste, pausa"); time.sleep(15); return []
        if r.status_code >= 400:
            return []
        out = []
        for it in r.json().get("itemSummaries", []) or []:
            titolo = it.get("title", "")
            if RUMORE.search(titolo):
                continue
            pr = it.get("price", {})
            try:
                val = float(pr.get("value"))
            except (TypeError, ValueError):
                continue
            val_cur = pr.get("currency", valuta)
            eur = round(val * CAMBI.get(val_cur, 1.0), 2)
            out.append({
                "id": it.get("itemId", ""),
                "titolo": titolo,
                "eur": eur,
                "mercato": mercato,
                "condizione": it.get("condition", "—"),
                "url": it.get("itemWebUrl", ""),
                "testo": (titolo + " " + (it.get("shortDescription") or "")).lower(),
            })
        return out


# ----------------------------------------------------------------------
# Compatibilità: regione + sottotitoli
# ----------------------------------------------------------------------

def valuta_compatibilita(testo: str, animazione: bool) -> tuple[str, str]:
    """Ritorna (stato, nota). stato: 'ok' | 'verifica' | 'scarta'."""
    # regione
    if REG_ALTRE.search(testo) and not REG_B.search(testo):
        return "scarta", "regione non compatibile"
    regione_ok = "Region B/free" if REG_B.search(testo) else "regione non dichiarata"

    sub_it = bool(SUB_IT.search(testo))
    sub_en = bool(SUB_EN.search(testo))

    if animazione:
        # serve almeno il sottotitolo/audio italiano
        if sub_it:
            return "ok", f"{regione_ok} · italiano presente"
        if sub_en:
            return "verifica", f"{regione_ok} · trovato inglese, italiano da verificare"
        return "verifica", f"{regione_ok} · sottotitoli da verificare (serve ITA)"
    else:
        # live action: va bene ita o eng
        if sub_it or sub_en:
            lingue = "italiano" if sub_it else "inglese"
            return "ok", f"{regione_ok} · {lingue} presente"
        return "verifica", f"{regione_ok} · sottotitoli da verificare"


def e_animazione(film: dict) -> bool:
    cat = (film.get("categoria") or film.get("tipo") or "").lower()
    testo = (film.get("titolo", "") + " " + film.get("note", "")).lower()
    indizi = ("animazione", "anime", "ghibli", "kung fu panda", "spider-verse",
              "airone", "flow", "pippo", "poppy")
    return "anim" in cat or any(x in testo for x in indizi)


# ----------------------------------------------------------------------
# Memoria anti-doppioni
# ----------------------------------------------------------------------

def leggi_visti() -> dict:
    if FILE_VISTI.exists():
        try:
            return json.loads(FILE_VISTI.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def scrivi_visti(v: dict) -> None:
    FILE_VISTI.write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# Messaggio
# ----------------------------------------------------------------------

def messaggio(film: dict, off: dict, stato: str, nota: str) -> str:
    e = lambda s: html.escape(str(s))
    _, _, bandiera = MERCATI_DISPONIBILI[off["mercato"]]
    icona = "✅" if stato == "ok" else "🔎"
    prezzo = f"{off['eur']:.2f}".replace(".", ",")
    return (
        f"{icona} <b>{e(film.get('titolo',''))}</b>\n"
        f"💶 <b>€{prezzo}</b> su eBay {bandiera} · {e(off['condizione'])}\n"
        f"💿 {e(nota)}\n"
        f"<i>{e(off['titolo'][:80])}</i>\n\n"
        f'<a href="{e(off["url"])}">Apri l\'annuncio</a>'
    )


# ----------------------------------------------------------------------
# Un giro
# ----------------------------------------------------------------------

def un_giro(ebay: EBay) -> None:
    if not FILE_LISTA.exists():
        print("Manca lista_desideri.json — esportalo dalla wishlist.")
        return
    dati = json.loads(FILE_LISTA.read_text(encoding="utf-8"))
    film = dati.get("desideri") or dati.get("items") or []
    film = [f for f in film if (f.get("categoria") or f.get("tipo") or "film") == "film"]
    if SOLO:
        film = [f for f in film if SOLO in (f.get("titolo", "") + f.get("titolo_originale", "")).lower()]
    if not film:
        print("Nessun film da cercare.")
        return

    visti = leggi_visti()
    nuovi_avvisi = 0
    print(f"\n=== Giro eBay {datetime.now():%d/%m %H:%M} — {len(film)} film, mercati {','.join(MERCATI)} ===")

    for f in film:
        titolo = f.get("titolo", "")
        anim = e_animazione(f)
        query_it = f.get("ricerca_it") or titolo
        query_int = f.get("ricerca") or query_it
        print(f"  {titolo[:45]:<45} {'[anim]' if anim else '      '}", end="", flush=True)

        offerte = []
        for mercato in MERCATI:
            q = query_it if mercato == "IT" else query_int
            offerte += ebay.cerca(q, mercato)
            time.sleep(0.5)

        segnalate = 0
        for off in sorted(offerte, key=lambda o: o["eur"]):
            if off["id"] in visti:
                continue
            stato, nota = valuta_compatibilita(off["testo"], anim)
            if stato == "scarta":
                continue
            visti[off["id"]] = {"titolo": titolo, "eur": off["eur"],
                                "quando": datetime.now().isoformat(timespec="seconds")}
            telegram(messaggio(f, off, stato, nota))
            nuovi_avvisi += 1
            segnalate += 1
            time.sleep(1)
            if segnalate >= 3:   # max 3 nuove offerte per film per giro, niente spam
                break
        print(f"  {segnalate} nuove")

    scrivi_visti(visti)
    print(f"=== Fine: {nuovi_avvisi} nuove offerte segnalate ===")


# ----------------------------------------------------------------------
# Avvio
# ----------------------------------------------------------------------

def main() -> int:
    print("Bot eBay — sorveglianza lista desideri")
    problemi = []
    if not (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET):
        problemi.append("Mancano EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (developer.ebay.com, chiavi Production)")
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        problemi.append("Mancano TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    if not MERCATI:
        problemi.append("Nessun mercato valido in MERCATI")
    if problemi:
        print("Non posso partire:")
        for p in problemi:
            print("  -", p)
        return 1

    ebay = EBay()
    telegram("🟢 Bot eBay avviato. Ti avviso quando trovo dischi Region B/free compatibili sui tuoi film.")

    while True:
        try:
            un_giro(ebay)
        except Exception as e:
            print("Errore nel giro:", e)
            telegram(f"⚠️ Bot eBay, problema in un giro: {html.escape(str(e))}")
        if SOLO_UNA_VOLTA:
            return 0
        print(f"Aspetto {ORE_TRA_I_GIRI} ore.\n")
        time.sleep(ORE_TRA_I_GIRI * 3600)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nFermato.")
        sys.exit(130)
