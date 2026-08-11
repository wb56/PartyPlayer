# DeckRelay – Performance-Stabilisierung Phase 2D

## Ziel

Phase 2D entfernt Datei-, Tag-, NAS- und Datenbankzugriffe aus dem GUI-Callback eines
abgeschlossenen Preloads. Playback-, Deck-, Crossfade-, Queue-Auswahl- und
Datenbankschema bleiben fachlich unverändert.

## Vorbereiteter Preload

Der Preloadworker erzeugt vor der GUI-Veröffentlichung ein unveränderliches
`PreparedPreloadResult` mit:

- vorbereitetem VLC-Medium,
- vollständig aufgelösten `ResolvedLoudnessSettings`,
- vollständig aufgelösten Cue-Grenzen.

Die Lautheitsauflösung darf SQLite, ReplayGain-Tags und Audiodateien verwenden, läuft
aber unter `worker.preload.resolve_loudness`. Das Ergebnis enthält angeforderten und
effektiven Gain, linearen Faktor, Quelle, Peak-Begrenzung und Normalisierungsmodus.

## GUI-Phase

Der GUI-Callback führt nur folgende geordnete Schritte aus:

1. Generation und gecachten Queuezustand prüfen.
2. Vorbereitetes VLC-Medium dem freien Deck zuweisen.
3. Vorbereitete Cue-Grenzen in das Deckmodell kopieren.
4. Vorbereiteten linearen Gain an den nicht blockierenden Volume-Pfad übergeben.
5. Coverjob an den begrenzten Coverexecutor übergeben.
6. Queue-Persistenz an den Preloadexecutor übergeben.

Der Gain-Befehl wird separat als `audio.apply_gain_command` gemessen. Das VLC-Backend
nimmt Lautstärkeänderungen über seinen bestehenden Volume-Worker entgegen; der
Tk-Hauptthread wartet nicht auf den nativen VLC-Lautstärkebefehl.

## Persistenz- und Folgephase

Deckzuordnungen und Queue-Status werden nach der Medienübernahme im
`preload-worker` persistiert. Erst nach erfolgreicher Persistenz wird der benannte
Dispatcherauftrag `preload_followup` veröffentlicht. Er aktualisiert Queue-/Deckansicht
und setzt den unveränderten Automatikablauf fort.

Ein Persistenzfehler wird als eigener GUI-Auftrag gemeldet, beendet den
Preloadzustand kontrolliert und startet keinen Folgetitel. Dadurch bleibt die
fachliche Reihenfolge erhalten, ohne SQLite im GUI-Callback aufzurufen.

## Coverauftrag

Der GUI-Schritt `schedule_cover` übergibt nur Deck-ID, Track-ID/Trackpfad und eine
Operation-ID an einen festen `ThreadPoolExecutor`. Der Worker erledigt:

- eingebettete und alternative Coversuche,
- Datei- und NAS-Prüfungen,
- Metadatenlesen,
- Dekodierung, Konvertierung und Skalierung.

Nur `CTkImage`-Erzeugung und Widgetkonfiguration laufen weiterhin in Tk.

## Benannte GUI-Aufträge

Der Dirty-Row-Scheduler erzeugt benannte Funktionen statt instrumentierter Lambdas:

```text
gui_callback.after.catalog_render_chunk
gui_callback.after.queue_render_chunk
```

Normalisierungs- und Fade-Ticks sowie Transition-Prüfungen besitzen ebenfalls
stabile Funktionsnamen. Sollte dennoch eine nicht kritische Lambda registriert werden,
wird sie im Messsystem als `anonymous_callback` statt als `<lambda>` ausgewiesen.

## Unterschiedliche Renderbudgets

Der Scheduler erhält eine seitenspezifische `is_creation`-Prüfung:

- neue Zeilen: maximal eine pro Chunk,
- Rebinding: maximal fünf Zeilen,
- beide Pfade: maximal 8 ms aktive Arbeit,
- Folgechunk: frühestens nach 10 ms.

Die Katalogreihenfolge priorisiert automatisch die ersten 15 Zeilen. Gemessen werden:

```text
gui.catalog_render.first_row_visible
gui.catalog_render.initial_visible_rows_complete
gui.catalog_render.full_page_complete
```

Die komplette Seite darf sich über mehrere Sekunden aufbauen, ohne einen einzelnen
GUI-Zyklus entsprechend lange zu blockieren.

## Heartbeat-Kontext

Bei jeder vom Tk-Heartbeat beobachteten Verzögerung ab 250 ms protokolliert der
Controller:

- zuletzt gestarteten GUI-Callback,
- zuletzt abgeschlossenen GUI-Callback,
- aktuell aktive GUI-Arbeit,
- aktiven Katalog-Renderauftrag,
- aktiven Queue-Renderauftrag.

Bei mindestens 750 ms erfasst der unabhängige Watchdog weiterhin den tatsächlichen
MainThread-Stack während der Blockade.

## Abnahmegrenzen

Unit-Tests beweisen Thread-/GUI-Trennung, benannte Scheduleraufträge,
Erstellungs-/Rebindingbudgets und unverändertes Playbackverhalten. Zeitziele wie
`preload.total` unter 25 ms oder Heartbeat unter 250 ms müssen mit realem VLC, Tk,
Audioausgang und gegebenenfalls NAS abgenommen werden. Sie werden nicht allein aus
automatisierten Tests als bestanden erklärt.
