# Automatische Cue-Analyse

PartyPlayer kann Anfang und Ende eines Titels offline anhand des Audiosignals
vorschlagen. Die Analyse verändert keine Musikdatei und läuft getrennt von der
Wiedergabe in einem Hintergrundworker.

## Funktionsweise

Standardmäßig werden nur die ersten und letzten 45 Sekunden untersucht. FFprobe
ermittelt die technischen Dateidaten; FFmpeg dekodiert die gewählten Abschnitte in
kleinen PCM-Blöcken. PartyPlayer berechnet daraus Pegelfenster, RMS, dBFS und Peak.

Eine Hysterese verwendet getrennte Schwellen für Signalbeginn und Signalende.
Mindestdauern unterdrücken kurze Pegelspitzen und verhindern, dass ein kurzer leiser
Einbruch eine zusammenhängende Musikpassage teilt. Das erste bestätigte Signal im
Anfangsfenster wird als Cue In vorgeschlagen, das letzte bestätigte Signal im
Endfenster als Cue Out. Ohne belastbare Erkennung bleibt die jeweilige Dateigrenze
erhalten.

Automatische Ergebnisse werden mit Pegeln, Konfidenz, Analyseversion, Backend und
Zeitpunkt getrennt von manuellen Cue-Werten gespeichert. Manuelle Werte haben immer
Vorrang.

## Technische und semantische Grenzen

Die Analyse erkennt Signalpegel, aber nicht die Bedeutung des Tons. Folgende Inhalte
können deshalb nicht zuverlässig automatisch beurteilt werden:

- Applaus oder Publikumsgeräusche können wie ein musikalisches Intro oder Outro wirken.
- Sprache, Moderation und Ansagen können als gültiges Signal erkannt werden.
- Hall und Reverb können Cue Out deutlich hinter das musikalisch gewünschte Ende legen.
- Absichtliche Pausen innerhalb eines Intros oder Outros können je nach Länge als
  Stille gewertet werden.
- Sehr leise Musik, leise Intros oder lange Fade-outs können unter der eingestellten
  Pegelschwelle liegen.
- Einzelne laute Störgeräusche werden zwar durch Mindestdauern gefiltert, längere
  Geräusche können aber weiterhin als Musik gelten.
- Nahtlos ineinander übergehende Live-Aufnahmen besitzen möglicherweise keine technisch
  eindeutige Titelgrenze.
- Starke Dynamikkompression, Rauschen oder bereits normalisierte Dateien verändern die
  Aussagekraft fester dBFS-Schwellen.

Die angezeigte Konfidenz beschreibt nur, wie vollständig und eindeutig die technischen
Randbereiche erkannt wurden. Sie ist keine Wahrscheinlichkeit dafür, dass ein Cue
musikalisch oder dramaturgisch richtig liegt.

## Empfohlener Arbeitsablauf

1. Automatische Einzel- oder Kataloganalyse starten.
2. Vorschlag, Pegel und Konfidenz im Cue-Editor prüfen.
3. Cue In und Cue Out vorhören.
4. Passenden Vorschlag bewusst als manuelle Werte übernehmen.
5. Werte bei Sprache, Applaus, Hall, Pausen oder leisen Passagen korrigieren.

Ein automatischer Vorschlag darf insbesondere vor einer Veranstaltung nicht die
hörbare Prüfung kritischer Übergänge ersetzen.
