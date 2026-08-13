# Changelog

Alle wesentlichen Änderungen an DeckRelay werden in dieser Datei dokumentiert.

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
