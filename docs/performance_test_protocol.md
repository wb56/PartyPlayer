# PartyPlayer – Protokoll für manuellen Performance-Lasttest

## Testdaten

| Feld | Eintrag |
|---|---|
| Datum | |
| PartyPlayer-Version | |
| Computer | |
| Betriebssystem | |
| Audioausgabe | |
| Musikquelle lokal/NAS | |
| Testszenario | `idle` / `normal_playback` / `crossfade` / `queue_stress` / `nas_playback` / `cue_preview` / `directory_import` / `database_delay` |
| Startzeit | |
| Endzeit | |
| Anzahl Titelwechsel | |
| Anzahl Crossfades | |
| Anzahl Cue-Previews | |

## Diagnoseberichte

| Zeitpunkt | Pfad zum Bericht |
|---|---|
| Vor dem Test | |
| Nach dem Test | |

## Besondere Aktionen

- [ ] Intensiv im Katalog gescrollt
- [ ] Intensiv in der Queue gescrollt
- [ ] Queue mehrfach verändert oder gemischt
- [ ] Cue-Preview während der Wiedergabe benutzt
- [ ] Manuellen Seek ausgeführt
- [ ] Crossfader manuell bewegt
- [ ] Verzeichnis importiert
- [ ] NAS-Verbindung belastet oder verzögert
- [ ] Sonstige Aktion: 

## Beobachtungen

### Subjektive GUI-Verzögerungen

- Zeitpunkt:
- Dauer:
- betroffene Bedienung:
- Audio lief störungsfrei weiter: ja / nein / unklar

### Audio- und Übergangsverhalten

- Unterbrechungen:
- stockende Crossfades:
- verspäteter Deckstart:
- sonstige Auffälligkeiten:

## Ergebnis

- [ ] bestanden
- [ ] bestanden mit Auffälligkeiten
- [ ] nicht bestanden

Begründung und reproduzierbare Schritte:

## Hinweise zur Auswertung

Nur gemessene Zusammenhänge festhalten. Eine zeitliche Überschneidung ist noch kein
Ursachennachweis. Insbesondere SQLite, VLC oder NAS erst dann als Ursache benennen, wenn der
zugehörige Messpunkt im selben Zeitraum wiederholt auffällig war.
