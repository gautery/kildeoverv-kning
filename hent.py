"""
Kildeovervåkning for @valgogpolitikk.

Henter nytt fra kildene i kilder.yaml, merker sakene med temaer fra tema.yaml
og lagrer alt i data/. Lager også OVERSIKT.md, docs/feed.xml og
docs/saker.json (som nettsiden leser).

Kjøres automatisk hver time av GitHub Actions (.github/workflows/hent.yml).
Kan kjøres for hånd med:  python hent.py
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROT = Path(__file__).parent
DATA = ROT / "data"
DOCS = ROT / "docs"
OSLO = ZoneInfo("Europe/Oslo")
NA = datetime.now(timezone.utc)

BEHOLD_DAGER = 180          # hvor lenge saker blir liggende i arkivet
OVERSIKT_DAGER = 14         # hvor mange dager OVERSIKT.md viser
FEED_ANTALL = 150           # antall saker i docs/feed.xml
USER_AGENT = ("Mozilla/5.0 (compatible; kildeovervakning/1.0; "
              "+https://github.com/gautery/kildeoverv-kning)")

VIKTIGE_TYPER = {"NOU", "Stortingsmelding", "Proposisjon", "Riksrevisjonen"}

# Regjeringens kalenderhendelser («Ministeren møter …») er støy for oss.
KALENDER_MONSTER = re.compile(
    r"(minister(en)?|statsråd(en)?)\b.{0,40}\b("
    r"deltar|deltek|møter|møte med|besøker|besøkjer|holder|held|opnar|åpner|"
    r"markerer|er i|er på|avholder|innleder|svarer|tar imot|tek imot|reiser|til )"
    r"|^(innleder|møte i|møte om|til stede)\b|program veke|program uke|kalender for",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────── hjelpere

def les_yaml(navn: str):
    with open(ROT / navn, encoding="utf-8") as f:
        return yaml.safe_load(f)


def les_json(sti: Path, standard):
    if sti.exists():
        with open(sti, encoding="utf-8") as f:
            return json.load(f)
    return standard


def skriv_json(sti: Path, innhold) -> None:
    sti.parent.mkdir(parents=True, exist_ok=True)
    with open(sti, "w", encoding="utf-8") as f:
        json.dump(innhold, f, ensure_ascii=False, indent=1)
        f.write("\n")


def ren_tekst(tekst: str | None, maks: int = 300) -> str:
    if not tekst:
        return ""
    tekst = re.sub(r"<[^>]+>", " ", tekst)
    tekst = html.unescape(tekst)
    tekst = re.sub(r"\s+", " ", tekst).strip()
    if len(tekst) > maks:
        tekst = tekst[:maks].rsplit(" ", 1)[0].rstrip(",.;:–-") + " …"
    return tekst


def ren_lenke(url: str) -> str:
    """Fjerner sporingsparametere (utm_*) så samme sak får samme id."""
    deler = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(deler.query) if not k.lower().startswith("utm_")]
    return urlunsplit((deler.scheme, deler.netloc, deler.path, urlencode(q), ""))


def lag_id(*deler: str) -> str:
    return hashlib.sha1("|".join(deler).encode("utf-8")).hexdigest()[:16]


def dato_fra_entry(e) -> datetime | None:
    for felt in ("published_parsed", "updated_parsed", "created_parsed"):
        st = e.get(felt)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    # Eurostat: «2026-11-21 11:00:00.0» (Brussel-tid)
    for felt in ("published", "updated", "date"):
        verdi = e.get(felt)
        if verdi:
            m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", verdi)
            if m:
                lokal = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
                return lokal.replace(tzinfo=ZoneInfo("Europe/Brussels")).astimezone(timezone.utc)
    return None


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def fra_iso(tekst: str) -> datetime:
    return datetime.fromisoformat(tekst.replace("Z", "+00:00"))



# Stortingets strømmer har titler som «Dokument 8:54 S (2026-2027)». Den egentlige
# saken står i beskrivelsen, så vi lager en lesbar tittel av den.
STORTING_NUMMER = re.compile(r"^(Dokument \d+:\d+ \w+|Innst\. \d+ \w+|Lovvedtak \d+) \(\d{4}-\d{4}\)$")


def bedre_tittel(sak: dict) -> None:
    if not sak["kilde"].startswith("Stortinget"):
        return
    nummer = sak.get("dokument") or sak["tittel"]
    if not STORTING_NUMMER.match(nummer) or not sak.get("ingress"):
        return
    tekst = sak["ingress"].rstrip(" …")
    # «Representantforslag fra stortingsrepresentantene A, B og C om …» → «Representantforslag om …»
    tekst = re.sub(r"^(Representantforslag) fra stortingsrepresentant(?:ene|en) .+? om ", r"\1 om ", tekst)
    tekst = re.sub(r"^Innstilling fra (.+?komiteen) om Representantforslag fra .+? om ",
                   r"Innstilling fra \1 om representantforslag om ", tekst)
    tekst = re.sub(r"^Vedtak til lov om ", "Lovvedtak om ", tekst)
    sak["dokument"] = nummer
    sak["tittel"] = ren_tekst(tekst, 220)
    if not sak.get("type"):
        sak["type"] = nummer.split(" (")[0]

# ─────────────────────────────────────────────────────────── temaer

def bygg_temamatcher(tema_cfg: dict):
    matchere = []
    for tema, ord_liste in tema_cfg.items():
        mønstre = []
        for o in ord_liste:
            o = str(o).strip()
            if o.startswith("ord:"):
                mønstre.append(r"(?<![\wæøå])" + re.escape(o[4:].strip()) + r"(?![\wæøå])")
            else:
                mønstre.append(re.escape(o))
        matchere.append((tema, re.compile("|".join(mønstre), re.IGNORECASE)))
    return matchere


def finn_temaer(tekst: str, matchere) -> list[str]:
    return [tema for tema, m in matchere if m.search(tekst)]


def har_ord(tekst: str, ord_liste) -> bool:
    t = tekst.lower()
    return any(str(o).lower() in t for o in ord_liste)


# ─────────────────────────────────────────────────────────── kildetyper

def hent(url: str, forsøk: int = 3) -> requests.Response:
    """GET med noen nye forsøk – enkelte nettsteder (f.eks. Stortinget) avbryter av og til."""
    import time
    for i in range(forsøk):
        try:
            svar = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=40)
            svar.raise_for_status()
            return svar
        except requests.RequestException:
            if i == forsøk - 1:
                raise
            time.sleep(4 * (i + 1))


def hent_feed(url: str):
    svar = hent(url)
    feed = feedparser.parse(svar.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"klarte ikke å lese strømmen ({feed.bozo_exception})")
    return feed


def regjeringen_type(lenke: str, tittel: str) -> str | None:
    sti = urlsplit(lenke).path.lower()
    if "/sub/" in sti or "/tema/" in sti:
        return None                                  # EØS-notater, reiseinfo o.l.
    if "/dokumenter/nou-" in sti or tittel.startswith("NOU "):
        return "NOU"
    if "/dokumenter/horing" in sti or tittel.lower().startswith("høring"):
        return "Høring"
    if "/dokumenter/meld.-st" in sti or tittel.startswith("Meld. St."):
        return "Stortingsmelding"
    if "/dokumenter/prop." in sti or tittel.startswith("Prop."):
        return "Proposisjon"
    if "/dokumenter/" in sti:
        return "Dokument"
    if KALENDER_MONSTER.search(tittel):
        return None
    return "Nyhet"



# ─────────────────────────────────────────────────────────── spor

# Sakene deles i tre spor, fordi rapporter leses på en annen måte enn løpende nytt:
#   rapporter  – rapporter, utredninger og analyser (NOU, forskning, tenketanker, Riksrevisjonen)
#   tall       – ny statistikk og tall (SSB, Eurostat, Norges Bank)
#   lopende    – det som skjer fra dag til dag (Storting, regjering, partier, tilsyn)
# En kilde kan overstyre dette med «spor:» i kilder.yaml.
SPOR_ETTER_KATEGORI = {
    "Forskning": "rapporter", "Tenketank": "rapporter", "Kontroll": "rapporter",
    "Statistikk": "tall", "Kalender": "tall", "Norges Bank": "tall",
}
RAPPORT_TYPER = {"NOU", "Stortingsmelding", "Proposisjon"}


def finn_spor(sak: dict, overstyr: dict[str, str]) -> str:
    if sak["kilde"] in overstyr:
        return overstyr[sak["kilde"]]
    if sak.get("type") in RAPPORT_TYPER:
        return "rapporter"
    if sak["kilde"] == "SSB":
        sti = urlsplit(sak["lenke"]).path
        return "rapporter" if "/artikler/" in sti and "/statistikk/" not in sti else "tall"
    return SPOR_ETTER_KATEGORI.get(sak.get("kategori", ""), "lopende")


# ─────────────────────────────────────────────────────────── nettsider uten RSS

MÅNEDSNAVN = {m: i + 1 for i, m in enumerate(
    ["januar", "februar", "mars", "april", "mai", "juni", "juli",
     "august", "september", "oktober", "november", "desember"])}


def hent_tittel(side: str) -> str:
    """Henter artikkelens tittel (og:title eller <h1>) uten «| Nettstedsnavn»-hale."""
    m = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', side)
         or re.search(r"<h1[^>]*>(.*?)</h1>", side, re.S))
    if not m:
        return ""
    tittel = ren_tekst(m.group(1), 250)
    første = tittel.split(" | ")[0].strip()
    if len(første) >= 10:
        tittel = første
    return re.split(r"\s+[|–—-]\s+(?=[^|–—-]{2,40}$)", tittel)[0].strip()


def hent_artikkelinfo(lenke: str) -> tuple[datetime | None, str, str]:
    """Leser publiseringsdato, ingress og tittel fra en artikkelside."""
    try:
        svar = requests.get(lenke, headers={"User-Agent": USER_AGENT}, timeout=30)
        svar.raise_for_status()
    except Exception:
        return None, "", ""
    side = svar.text
    tittel = hent_tittel(side)
    ingress = ""
    m = re.search(r'<meta[^>]+(?:property|name)="(?:og:)?description"[^>]+content="([^"]*)"', side)
    if m:
        ingress = ren_tekst(m.group(1))
    for mønster in (r'article:published_time"[^>]*content="([^"]+)"',
                    r'"datePublished"\s*:\s*"([^"]+)"',
                    r'<time[^>]*datetime="([^"]+)"'):
        m = re.search(mønster, side)
        if m:
            try:
                dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                return (dt if dt.tzinfo else dt.replace(tzinfo=OSLO)), ingress, tittel
            except ValueError:
                pass
    # «Publisert: 12. januar 1998» foran andre datoer på siden (Fafo legger ut gamle rapporter fra arkivet).
    måneder = "|".join(MÅNEDSNAVN)
    m = re.search(r"Publisert:?\s*(?:<[^>]+>\s*)*(\d{1,2})\.\s*(" + måneder + r")\s+((?:19|20)\d\d)\b", side, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,2})\.\s*(" + måneder + r")\s+((?:19|20)\d\d)\b", side, re.IGNORECASE)
    if m:
        dag, mnd, år = int(m.group(1)), MÅNEDSNAVN[m.group(2).lower()], int(m.group(3))
        return datetime(år, mnd, dag, 12, tzinfo=OSLO), ingress, tittel
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.((?:19|20)\d\d)\b", side)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, tzinfo=OSLO), ingress, tittel
        except ValueError:
            pass
    return None, ingress, tittel


def behandle_nettside(kilde: dict, sett: dict[str, list[str]]):
    """Henter artikkellenker fra en vanlig nettside (for kilder uten RSS)."""
    svar = hent(kilde["url"])
    mønster = re.compile(kilde["lenke_mønster"])
    funn: dict[str, str] = {}
    for href, innhold in re.findall(r'<a\b[^>]*?href="([^"#]+)"[^>]*>(.*?)</a>', svar.text, re.S | re.I):
        lenke = ren_lenke(urljoin(kilde["url"], html.unescape(href)))
        if not mønster.search(lenke):
            continue
        tittel = ren_tekst(innhold, 250)
        if len(tittel) > len(funn.get(lenke, "")):
            funn[lenke] = tittel
    funn = {l: t for l, t in funn.items() if len(t) >= 15}
    if not funn:
        raise ValueError("fant ingen artikkellenker – har nettsiden endret seg?")

    første_gang = kilde["navn"] not in sett
    kjente = sett.setdefault(kilde["navn"], [])
    saker = []
    for lenke, tittel in list(funn.items())[:25]:
        sak_id = lag_id(lenke)
        if sak_id in kjente:
            continue
        kjente.append(sak_id)
        dato, ingress, sidetittel = hent_artikkelinfo(lenke)
        if sidetittel and len(sidetittel) >= 10:
            tittel = sidetittel      # lenketeksten på oversiktssiden kan inneholde ingress og dato
        if første_gang and dato is None:
            continue          # første kjøring: ikke fyll lista med udaterte eldre saker
        if ingress.strip(" .…") == tittel.strip(" .…"):
            ingress = ""
        saker.append(lag_sak(sak_id, kilde, tittel, lenke, ingress, dato, None))
    sett[kilde["navn"]] = kjente[-300:]
    return saker, []

def behandle_kilde(kilde: dict, ignorert: set[str], sett: dict[str, list[str]]):
    """Returnerer (saker, kalenderposter)."""
    typ = kilde.get("type", "rss")
    if typ == "nettside":
        return behandle_nettside(kilde, sett)
    if typ == "google_nyheter":
        # Google News-søk (f.eks. «site:frp.no») for nettsider som stenger ute automatiske besøk.
        from urllib.parse import quote
        kilde = {**kilde, "url": "https://news.google.com/rss/search?q=" + quote(kilde["søk"])
                 + "&hl=no&gl=NO&ceid=NO:no"}
    feed = hent_feed(kilde["url"])
    saker, kalender = [], []

    for e in feed.entries:
        tittel = ren_tekst(e.get("title"), 250)
        if typ == "google_nyheter":
            kildenavn = (e.get("source") or {}).get("title", "")
            if kildenavn and tittel.endswith(" - " + kildenavn):
                tittel = tittel[: -len(" - " + kildenavn)]
            for hale in kilde.get("fjern_fra_tittel", []):
                tittel = tittel.removesuffix(hale).strip()
        lenke = ren_lenke(e.get("link") or "")
        if not tittel or not lenke:
            continue
        ingress = "" if typ == "google_nyheter" else ren_tekst(e.get("summary") or e.get("description"))
        dato = dato_fra_entry(e)
        sak_type = None

        if typ == "ssb_kalender":
            m = re.match(r"(\d{2})\.(\d{2})\.(\d{4}):\s*(.+)", tittel)
            if m:
                d, mnd, år, rest = m.groups()
                kalender.append({"dato": f"{år}-{mnd}-{d}", "tittel": rest, "lenke": lenke})
            continue

        if typ == "regjeringen":
            sak_id = lag_id(lenke)
            if sak_id in ignorert:
                continue
            sak_type = regjeringen_type(lenke, tittel)
            # Saker datert fram i tid er kalenderhendelser – husk dem som ignorert.
            if sak_type is None or (dato and dato > NA + timedelta(hours=1)):
                ignorert.add(sak_id)
                continue

        elif typ == "eurostat":
            kode = tittel.split(" - ")[0].strip().upper()
            hendelse = e.get("category") or e.get("tags", [{}])[0].get("term", "")
            ny = "NEW" in str(hendelse).upper()
            på_lista = kode in {k.upper() for k in kilde.get("datasett", [])}
            if not (på_lista or (ny and har_ord(ingress, kilde.get("bare_med_ord", [])))):
                continue
            sak_type = "Nytt datasett" if ny else "Oppdaterte data"
            tittel = f"{ingress} ({kode})" if ingress else kode
            ingress = f"Eurostat-datasett {kode}" + (" er nytt." if ny else " har fått nye tall.")
            sak_id = lag_id(kode, iso(dato) or "")
            saker.append(lag_sak(sak_id, kilde, tittel, lenke, ingress, dato, sak_type))
            continue

        else:
            sak_id = lag_id(lenke)

        if kilde.get("bare_med_ord") and not har_ord(f"{tittel} {ingress}", kilde["bare_med_ord"]):
            continue
        saker.append(lag_sak(sak_id, kilde, tittel, lenke, ingress, dato, sak_type))

    return saker, kalender


def lag_sak(sak_id, kilde, tittel, lenke, ingress, dato, sak_type):
    # Datoer fram i tid (f.eks. høringsdato på Stortinget) sorteres som «funnet nå».
    if dato is None or dato > NA + timedelta(hours=1):
        dato = NA
    if kilde["navn"].startswith("Riksrevisjonen"):
        sak_type = "Riksrevisjonen"
    return {
        "id": sak_id,
        "tittel": tittel,
        "lenke": lenke,
        "ingress": ingress,
        "kilde": kilde["navn"],
        "kategori": kilde.get("kategori", ""),
        "type": sak_type or "",
        "dato": iso(dato),
        "viktig": bool(kilde.get("viktig")) or sak_type in VIKTIGE_TYPER,
        **({"tema_fra_tittel": True} if kilde.get("tema_fra_tittel") else {}),
    }


# ─────────────────────────────────────────────────────────── utdata

def lokal_dato(iso_tekst: str) -> datetime:
    return fra_iso(iso_tekst).astimezone(OSLO)


UKEDAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MÅNEDER = ["januar", "februar", "mars", "april", "mai", "juni", "juli",
           "august", "september", "oktober", "november", "desember"]


def pen_dag(d: datetime) -> str:
    return f"{UKEDAGER[d.weekday()].capitalize()} {d.day}. {MÅNEDER[d.month - 1]}"


def skriv_oversikt(saker, kalender, status, temaer) -> None:
    grense = NA - timedelta(days=OVERSIKT_DAGER)
    nye = [s for s in saker if fra_iso(s["dato"]) >= grense]
    l = ["# Kildeovervåkning", "",
         f"_Sist oppdatert {datetime.now(OSLO):%d.%m.%Y kl. %H:%M}. "
         f"Viser de siste {OVERSIKT_DAGER} dagene ({len(nye)} saker). "
         "★ = særlig viktig (NOU, melding, proposisjon, Riksrevisjonen)._", ""]

    viktige = [s for s in nye if s["viktig"]]
    if viktige:
        l += ["## ★ Viktigst", ""]
        for s in viktige[:15]:
            l.append(f"- **[{s['tittel']}]({s['lenke']})** – {s['type'] or s['kilde']}, "
                     f"{lokal_dato(s['dato']):%d.%m}")
        l.append("")

    kommende = [k for k in kalender if k.get("temaer")][:25]
    if kommende:
        l += ["## Kommer fra SSB (relevant for temaene dine)", ""]
        for k in kommende:
            d = datetime.fromisoformat(k["dato"])
            l.append(f"- **{d:%d.%m}** – [{k['tittel']}]({k['lenke']}) · _{', '.join(k['temaer'])}_")
        l.append("")

    l += ["## Alt nytt, dag for dag", ""]
    dag = None
    for s in nye:
        d = lokal_dato(s["dato"])
        if d.date() != dag:
            dag = d.date()
            l += ["", f"### {pen_dag(d)}", ""]
        stjerne = "★ " if s["viktig"] else ""
        etikett = f"{s['kilde']}" + (f" · {s['type']}" if s["type"] and s["type"] != "Nyhet" else "")
        tema = f" · _{', '.join(s['temaer'])}_" if s["temaer"] else ""
        l.append(f"- {stjerne}[{s['tittel']}]({s['lenke']}) — {etikett}{tema}")

    feil = [s for s in status if not s["ok"]]
    l += ["", "## Status for kildene", ""]
    if feil:
        l.append("Disse kildene feilet ved siste kjøring:")
        for s in feil:
            l.append(f"- ⚠️ **{s['navn']}**: {s['feil']}")
    else:
        l.append(f"Alle {len(status)} kildene svarte normalt ved siste kjøring.")
    l.append("")
    (ROT / "OVERSIKT.md").write_text("\n".join(l), encoding="utf-8")


SPOR_NAVN = {"rapporter": "Rapporter og utredninger", "tall": "Statistikk og tall", "lopende": "Løpende nytt"}


def skriv_feed(saker, filnavn: str = "feed.xml", undertittel: str = "") -> None:
    deler = ['<?xml version="1.0" encoding="utf-8"?>',
             '<rss version="2.0"><channel>',
             f"<title>Kildeovervåkning{(' – ' + undertittel) if undertittel else ''} – valgogpolitikk</title>",
             "<link>https://github.com/gautery/kildeoverv-kning</link>",
             "<description>Nytt fra regjeringen, Stortinget, SSB, Eurostat, tilsyn, partier og forskning</description>",
             "<language>no</language>"]
    for s in saker[:FEED_ANTALL]:
        kat = "".join(f"<category>{escape(t)}</category>" for t in s["temaer"])
        deler.append(
            "<item>"
            f"<title>{escape(s['tittel'])}</title>"
            f"<link>{escape(s['lenke'])}</link>"
            f"<guid isPermaLink=\"false\">{s['id']}</guid>"
            f"<pubDate>{format_datetime(fra_iso(s['dato']))}</pubDate>"
            f"<description>{escape((s['kilde'] + ': ' + s['ingress']).strip(': '))}</description>"
            f"{kat}</item>")
    deler.append("</channel></rss>")
    DOCS.mkdir(exist_ok=True)
    (DOCS / filnavn).write_text("\n".join(deler), encoding="utf-8")


# ─────────────────────────────────────────────────────────── hovedløp


# ─────────────────────────────────────────────────────────── utvalgte saker

def les_utvalgt(arkiv: dict, matchere) -> list[dict]:
    """Leser utvalgt.yaml: saker Gaute vil fremheve på forsiden, med egen kommentar."""
    sti = ROT / "utvalgt.yaml"
    if not sti.exists():
        return []
    innhold = yaml.safe_load(sti.read_text(encoding="utf-8")) or {}
    etter_lenke = {s["lenke"]: s for s in arkiv.values()}
    ut = []
    for u in innhold.get("utvalgt") or []:
        if not u or not u.get("lenke"):
            continue
        lenke = ren_lenke(str(u["lenke"]))
        sak = etter_lenke.get(lenke, {})
        dato = u.get("dato")
        if isinstance(dato, str):   # redigeringsverktøyet kan lagre datoen som tekst
            try:
                dato = datetime.strptime(dato.strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                dato = None
        ut.append({
            "id": lag_id("utvalgt", lenke),
            "tittel": str(u.get("tittel") or sak.get("tittel") or lenke),
            "lenke": lenke,
            "kilde": str(u.get("kilde") or sak.get("kilde") or urlsplit(lenke).netloc.replace("www.", "")),
            "kommentar": ren_tekst(str(u.get("kommentar") or ""), 400),
            "dato": iso(datetime.combine(dato, datetime.min.time(), OSLO)) if hasattr(dato, "year")
                    else (sak.get("dato") or iso(NA)),
            "temaer": sak.get("temaer") or finn_temaer(str(u.get("tittel") or ""), matchere),
        })
    return ut


def main() -> int:
    kilder = les_yaml("kilder.yaml")["kilder"]
    matchere = bygg_temamatcher(les_yaml("tema.yaml"))

    gamle = les_json(DATA / "saker.json", [])
    arkiv = {s["id"]: dict(s) for s in gamle}   # kopi, så endringer kan sammenlignes med «gamle»
    ignorert_liste = les_json(DATA / "ignorert.json", [])
    ignorert = set(ignorert_liste)
    for_ignorert = set(ignorert)
    gammel_status = les_json(DATA / "status.json", [])
    gammel_kalender = les_json(DATA / "kalender.json", [])
    sett = les_json(DATA / "sett_nettsider.json", {})
    gammelt_sett = json.dumps(sett, sort_keys=True)

    grense = NA - timedelta(days=BEHOLD_DAGER)
    status, kalender, antall_nye = [], [], 0
    for kilde in kilder:
        if kilde.get("aktiv") is False:
            continue
        try:
            saker, kal = behandle_kilde(kilde, ignorert, sett)
            nye_her = 0
            for s in saker:
                if fra_iso(s["dato"]) < grense:
                    continue                      # for gammel til å tas med
                if s["id"] not in arkiv:
                    s["funnet"] = iso(NA)
                    arkiv[s["id"]] = s
                    nye_her += 1
            kalender += kal
            antall_nye += nye_her
            status.append({"navn": kilde["navn"], "ok": True, "feil": None})
            print(f"  ok   {kilde['navn']}: {len(saker) + len(kal)} i strømmen, {nye_her} nye")
        except Exception as feil:  # én kilde som feiler skal ikke stoppe resten
            melding = str(feil)[:200]
            status.append({"navn": kilde["navn"], "ok": False, "feil": melding})
            print(f"  FEIL {kilde['navn']}: {melding}")

    # Temaer settes på nytt hver gang, så endringer i tema.yaml slår inn bakover.
    bare_tittel = {k["navn"] for k in kilder if k.get("tema_fra_tittel")}
    overstyr_spor = {k["navn"]: k["spor"] for k in kilder if k.get("spor")}
    for s in arkiv.values():
        bedre_tittel(s)
        tekst = s["tittel"] if (s.get("tema_fra_tittel") or s["kilde"] in bare_tittel) else f"{s['tittel']} {s['ingress']}"
        s["temaer"] = finn_temaer(tekst, matchere)
        s["spor"] = finn_spor(s, overstyr_spor)
    for k in kalender:
        k["temaer"] = finn_temaer(k["tittel"], matchere)
    kalender.sort(key=lambda k: k["dato"])

    # Saker fra kilder som er skrudd av eller fjernet, tas ut av oversikten.
    aktive = {k["navn"] for k in kilder if k.get("aktiv") is not False}
    saker = [s for s in arkiv.values() if fra_iso(s["dato"]) >= grense and s["kilde"] in aktive]
    saker.sort(key=lambda s: s["dato"], reverse=True)

    utvalgt = les_utvalgt(arkiv, matchere)
    gammel_utvalgt = les_json(DOCS / "saker.json", {}).get("utvalgt") if (DOCS / "saker.json").exists() else None

    endret = (saker != gamle or kalender != gammel_kalender or status != gammel_status
              or utvalgt != gammel_utvalgt or not (ROT / "OVERSIKT.md").exists())
    print(f"{antall_nye} nye saker, {len(saker)} totalt i arkivet.")

    if json.dumps(sett, sort_keys=True) != gammelt_sett:
        skriv_json(DATA / "sett_nettsider.json", sett)
    nye_ignorert = [i for i in ignorert if i not in for_ignorert]
    if nye_ignorert:
        skriv_json(DATA / "ignorert.json", (ignorert_liste + sorted(nye_ignorert))[-5000:])
    if not endret:
        print("Ingen endringer – lar filene stå.")
        return 0

    skriv_json(DATA / "saker.json", saker)
    skriv_json(DATA / "kalender.json", kalender)
    skriv_json(DATA / "status.json", status)
    skriv_json(DOCS / "saker.json", {"oppdatert": iso(NA), "utvalgt": utvalgt, "saker": saker,
                                    "kalender": kalender, "status": status})
    skriv_feed(saker)
    for spor, navn in SPOR_NAVN.items():
        skriv_feed([x for x in saker if x.get("spor") == spor], f"feed-{spor}.xml", navn)
    skriv_oversikt(saker, kalender, status, matchere)
    return 0


if __name__ == "__main__":
    sys.exit(main())
