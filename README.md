# Kildeovervåkning

Automatisk oversikt over nytt fra gode kilder for [@valgogpolitikk](https://www.instagram.com/valgogpolitikk/): regjeringen (NOU-er, høringer, meldinger, pressemeldinger), Stortinget, Riksrevisjonen, SSB, Eurostat, tilsyn, Norges Bank, partier og forskning.

**➡️ [Les oversikten (OVERSIKT.md)](OVERSIKT.md)**

## Hvordan det virker

1. Hver time starter GitHub en liten robot (se fanen **Actions**).
2. Roboten kjører `hent.py`, som leser alle kildene i `kilder.yaml`.
3. Nye saker merkes med temaer fra `tema.yaml` og lagres i `data/saker.json`.
4. Roboten oppdaterer `OVERSIKT.md`, nettsiden i `docs/` og RSS-strømmen `docs/feed.xml`.

Ingenting av dette koster penger.

## Det du selv kan endre

| Vil du … | Gjør dette |
|---|---|
| legge til en kilde | Åpne `kilder.yaml`, trykk blyanten, kopier en blokk og endre navn/url. Trykk **Commit changes**. |
| skru av en kilde | Legg til linjen `aktiv: false` under kilden. |
| endre temaer | Åpne `tema.yaml` og legg til eller fjern ord. Endringen gjelder også gamle saker. |
| fremheve en sak på forsiden | Åpne `utvalgt.yaml`, lim inn lenken og en kort kommentar. Forklaring står øverst i fila. |
| legge til en nettside uten RSS | Bruk `type: nettside` og et `lenke_mønster`, se partiene i `kilder.yaml`. |
| kjøre med en gang | Gå til **Actions** → «Hent nytt fra kildene» → **Run workflow**. Det skjer også automatisk når du endrer `kilder.yaml`, `tema.yaml` eller `utvalgt.yaml`. |

## Filene

| Fil | Hva |
|---|---|
| `kilder.yaml` | Kildelisten |
| `tema.yaml` | Temaer og ordene som utløser dem |
| `utvalgt.yaml` | Saker du fremhever på forsiden av valgogpolitikk.no |
| `hent.py` | Programmet som henter og sorterer |
| `OVERSIKT.md` | Lesbar oversikt over de siste 14 dagene |
| `data/saker.json` | Alle saker fra de siste 180 dagene |
| `data/kalender.json` | Kommende SSB-publiseringer |
| `data/status.json` | Hvilke kilder som svarte ved siste kjøring |
| `docs/` | Nettsiden (GitHub Pages) og RSS-strømmen |

## Data og lisenser

Innholdet tilhører kildene og lenkes alltid til originalen. Offentlige data fra SSB, Eurostat og regjeringen kan brukes fritt med kildehenvisning (NLOD / CC BY 4.0).
