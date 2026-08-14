# Changelog

Alle wesentlichen Änderungen an DeckRelay werden in dieser Datei dokumentiert.

## [Unveröffentlicht]

### Fixed

- Beim erstmaligen Öffnen der Jingle- und Effektverwaltung wird ein neues Overlay
  vollständig mit gültigen Standardwerten vorbelegt: 75 % Lautstärke, 300 ms
  Fade-in, 500 ms Fade-out, Cue-In `00:00`, kein Cue-Out, -8 dB Musikabsenkung,
  200 ms Attack und 500 ms Release.
- Leere oder ungültige Zahlenfelder melden jetzt das betroffene Feld mit einem
  verständlichen Eingabehinweis, statt einen internen Python-Fehler wie
  `invalid literal for int()` anzuzeigen.
- Die Vorschau vor dem Automatikstart prüft alle Queue-Kandidaten über eine
  gemeinsame SQLite-Verbindung. Dadurch entfallen Verbindungsaufbau und
  -abbau pro Titel, die den GUI-Thread bei größeren Queues mehrere Sekunden
  blockieren konnten.
- Die Beta-2-Windows-EXE enthält nun eine konsistente Datei- und Produktversion;
  das irrtümlich weitergeführte portable Beta-1-Paket wurde entfernt.

### Added

- Der Messpunkt `automatic_start.summary` erfasst die vollständige Dauer der
  Automatikvorschau und warnt ab 50 ms.
- Ein Windows-GitHub-Actions-Workflow führt Ruff, Black, MyPy und die vollständige
  Pytest-Suite mit festgelegten Werkzeugversionen aus.
- Recovery-Regressionstests decken beide Deckrichtungen, idempotente History,
  verspätete Preload-Ergebnisse und aufeinanderfolgende nicht startende Titel ab.

## [1.0.0-beta.2] - 2026-08-13

### Fixed

- Titel werden automatisch übersprungen, wenn VLC auf dem eingehenden Deck trotz
  Startbefehl keine tatsächliche Wiedergabe bestätigt.
- Nach einem nicht bestätigten Deckwechsel bleibt die Automatik aktiv und setzt die
  Wiedergabe sicher im Ein-Deck-Betrieb auf dem funktionsfähigen Deck fort.
- Das fehlerhafte Deck und seine Queue-Zuordnung werden freigegeben, damit der
  nächste spielbare Titel wieder vorbereitet werden kann.

### Changed

- Versionsnummer und Windows-Laufzeit wurden auf `1.0.0-beta.2` aktualisiert.

## [1.0.0-beta.1] - 2026-08-11

### Changed

- Der öffentliche Produktname wurde von PartyPlayer in DeckRelay geändert.

### Added

- Erste öffentliche Beta-Version von DeckRelay.
