# PartyPlayer – Protokoll für den Speicher-Langzeittest

## Testdaten

- Datum:
- Version:
- Computer/Betriebssystem:
- Lokale Musik oder NAS:
- Audioausgabe:
- Diagnosebericht vor Aufwärmphase:
- Diagnosebericht nach Aufwärmphase:
- Diagnosebericht nach Zyklus 1:
- Diagnosebericht nach Zyklus 2:
- Diagnosebericht nach Zyklus 3:

## Aufwärmphase

- [ ] Automatik starten und mindestens 10 Titelwechsel durchführen.
- [ ] Katalog und Queue einmal vollständig bedienen.
- [ ] Szenario **Speicher-Langzeittest** auswählen und starten/resetten.

## Gesamtlast – mindestens

- [ ] 100 Titelwechsel
- [ ] 100 Preloads
- [ ] 100 Coverwechsel
- [ ] 50 Cue-Vorschauen
- [ ] 50 Queueänderungen
- [ ] 20 Queue-Neudarstellungen
- [ ] 10 Deck-Neustarts
- [ ] 3 vollständige Audio-Backend-Neuinitialisierungen

Die Gesamtlast möglichst gleichmäßig in drei identische Zyklen teilen. Nach jedem
Zyklus einen Bericht erzeugen beziehungsweise den RSS-/Heap-Zwischenstand notieren.

## Messwerte je Zyklus

| Wert | Aufwärmphase | Zyklus 1 | Zyklus 2 | Zyklus 3 |
|---|---:|---:|---:|---:|
| Prozess-RSS | | | | |
| Python-Heap | | | | |
| Python-Peak | | | | |
| Threads | | | | |
| aktive Worker | | | | |
| GUI-Queue | | | | |
| Covercache | | | | |
| Widgets | | | | |
| Tooltips | | | | |
| VLC-Player | | | | |

## Bewertung

- [ ] Worker, GUI-Queue, Cover, Widgets, Tooltips und VLC-Player kehren nach jedem
      Zyklus auf einen stabilen Bereich zurück.
- [ ] Ein einmaliger Cacheanstieg stabilisiert sich nach der Aufwärmphase.
- [ ] Heap und RSS wachsen nicht in jedem identischen Zyklus annähernd linear weiter.
- [ ] Tracemalloc-Zuwächse sind fachlich erklärt oder als Fehler erfasst.
- [ ] Ein RSS-Anstieg ohne vergleichbaren Heapanstieg wurde als native Ressource
      untersucht.
- [ ] Wiedergabe und GUI blieben während des Tests funktionsfähig.

Ergebnis/Beobachtungen:

```text

```
