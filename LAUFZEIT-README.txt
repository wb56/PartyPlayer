DECKRELAY - PORTABLE WINDOWS-VERSION

Start:
  DeckRelay.exe doppelt anklicken.

Installation auf einem anderen Rechner:
  Den gesamten Ordner DeckRelay kopieren. Einzelne Dateien aus dem Ordner
  duerfen nicht entfernt oder verschoben werden.

Voraussetzungen:
  Windows 10 oder Windows 11, 64 Bit.
  VLC/libVLC muss fuer die Wiedergabe separat installiert sein. DeckRelay
  erkennt uebliche Windows-Standardinstallationen und VLC im PATH automatisch.
  Ein alternativer VLC-Installationsordner kann im Einrichtungsassistenten oder
  unter "Einstellungen -> System / Externe Programme" ausgewaehlt werden.
  Fuer neue automatische Cue-/Lautheitsanalysen werden FFmpeg und FFprobe aus
  demselben separat installierten bin-Ordner benoetigt. Ohne FFmpeg bleiben
  Wiedergabe und bereits gespeicherte Analysewerte nutzbar.

  DeckRelay liefert VLC, libVLC, VLC-Plugins, FFmpeg und FFprobe nicht mit und
  laedt oder installiert diese Programme nicht automatisch. Downloadseiten werden
  ausschliesslich nach einer Benutzeraktion im Standardbrowser geoeffnet.
  Bestaetigte Pfadaenderungen werden nach einem Neustart wirksam.

Programmdaten:
  data\party_player.db       Musikbibliothek und Einstellungen
  logs\party_player.log     Protokolldatei
  diagnostics\              Diagnoseberichte und Watchdog-Dateien

Beim Aktualisieren der Anwendung sollte der Ordner data vorher gesichert und
anschliessend in die neue Version uebernommen werden.

Automatikmodus und CD-Queues:
  Die ausfuehrliche Anleitung steht in docs\automatic_playback.md.
  Fuer eine vollstaendige CD wird "Queue ersetzen" und danach
  "Vollstaendig abspielen" empfohlen. "Anhaengen" behaelt vorhandene Titel
  vor der neuen CD. Eine Deck-Pause pausiert die Automatik; Fortsetzen des
  Decks setzt auch die Automatik fort.

Equalizer und Lautstaerke:
  Rock, Pop, Bluesrock und Dance verwenden einen Sicherheits-Preamp von -3 dB.
  Deshalb kann die Wiedergabe beim Aktivieren eines Presets hoerbar leiser werden,
  obwohl Deckregler, Crossfader und Master unveraendert bleiben. Equalizer und
  Lautheitsnormalisierung arbeiten dabei als getrennte Verarbeitungsstufen.
