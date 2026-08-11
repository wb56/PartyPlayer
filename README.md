# Party Player

Eigenständige Windows-Anwendung für Musiksuche, Party-Warteschlange, Musikwünsche und Wiedergabe. Das Projekt verwendet eine eigene SQLite-Datenbank und verändert keine Musikdateien oder MP3-Tags.

## Entwicklung starten

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m party_player
```

Die Datenbank wird beim ersten Start unter `data/party_player.db` angelegt.

## Projektstatus

PartyPlayer befindet sich mit `v1.0.0-beta.1` in der öffentlichen Beta-Abnahme.
Reale Audio-, Geräte-, NAS-, Skalierungs- und Langzeittests sind weiterhin erforderlich.

- Quellcode und kostenlose Windows-Beta-Releases sind öffentlich verfügbar.
- Fehler, Missverständnisse und Funktionswünsche sollen über Issues gemeldet werden.
- Externe Code-Beiträge per Pull Request sind willkommen. Vor größeren Änderungen
  sollte zunächst ein Issue zur Abstimmung eröffnet werden.

Details stehen in [CONTRIBUTING.md](CONTRIBUTING.md).

## Lizenz

Dieses Projekt steht unter der GNU General Public License v3.0 oder einer
späteren Version (`GPL-3.0-or-later`). Details siehe [LICENSE](LICENSE).
Hinweise zu Drittanbieter-Lizenzen und Laufzeitabhaengigkeiten stehen in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## Laufzeitvoraussetzungen fuer Releases

- Windows 10 oder Windows 11, 64 Bit.
- VLC/libVLC ist fuer jede Wiedergabe erforderlich und muss separat installiert
  werden. PartyPlayer prueft zuerst einen bewusst gewaehlten Installationsordner,
  danach gaengige Windows-Standardinstallationen und anschliessend `PATH`.
- FFmpeg und FFprobe sind nur fuer neue automatische Cue- und Lautheitsanalysen
  erforderlich. Wiedergabe sowie vorhandene gespeicherte Analysewerte funktionieren
  auch ohne diese Werkzeuge. Beide Programme muessen aus demselben `bin`-Ordner
  stammen.
- Alternative VLC- und FFmpeg-Verzeichnisse koennen im Einrichtungsassistenten oder
  unter `Einstellungen -> System / Externe Programme` ausgewaehlt und validiert
  werden. Aenderungen gelten nach einem Neustart.
- VLC, libVLC, VLC-Plugins, FFmpeg und FFprobe werden weder mitgeliefert noch von
  PartyPlayer heruntergeladen oder installiert. Downloadseiten werden nur nach einer
  ausdruecklichen Benutzeraktion im Standardbrowser geoeffnet.

## Architektur

`UI -> Controller -> Service -> Repository -> SQLite`

- `ui`: Darstellung und Benutzerinteraktion
- `controllers`: Koordination der Oberfläche
- `services`: Geschäftsregeln
- `repositories`: ausschließlich Datenbankzugriffe
- `database`: Verbindung und Migrationen
- `player`: gekapselte Wiedergabe (wird in der nächsten Ausbaustufe ergänzt)

Funktionsweise, Bedienung und technische Grenzen der automatischen Cue-Erkennung sind
in [`docs/automatic_cue_analysis.md`](docs/automatic_cue_analysis.md) beschrieben.

Die benutzersichere Bedienung von Automatikmodus, Queue ersetzen/anhängen,
vollständiger CD-Reihenfolge, Pause/Fortsetzen und Cue-Fallback ist in
[`docs/automatic_playback.md`](docs/automatic_playback.md) beschrieben.

Gain-Reihenfolge, ReplayGain-Priorität, Peak-Schutz, Headroom und unveränderte
Quelldateien sind in
[`docs/loudness_normalization.md`](docs/loudness_normalization.md) dokumentiert.
