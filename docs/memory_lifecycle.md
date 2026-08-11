# DeckRelay – Speicher- und Ressourcenlebenszyklus

## Besitzer und Grenzen

| Ressource | Besitzer | Grenze | Freigabe |
|---|---|---:|---|
| GUI-Ereignisse | `GuiEventDispatcher` | 1000 | Verarbeitung/Verwerfen, Controllerende |
| Coveraufträge | `MainController` | 100 laufend + wartend, 2 Worker | Executor-Shutdown |
| Preloadaufträge | `MainController` | 2 laufend + wartend, 1 Worker | Abbruchgeneration, Shutdown |
| Playbackpersistenz | `MainController` | 500 laufend + wartend, 1 Worker | geordnetes Shutdown |
| Cue-Vorschauen | `CuePointController` | 2 laufend + wartend, 1 Worker | Stop-Event, Backend-`close()`, Shutdown |
| Performance-Einzelwerte | `PerformanceMonitor` | 500 je Messpunkt | Statistikreset/Monitorlebenszyklus |
| Workerhistorie | `WorkerRegistry` | 500 | automatisches Ringpufferlimit/Reset |
| Speichermessungen | `MemoryMonitor` | 500 | `reset()`/`close()` |
| Tracemalloc-Zuwächse | `MemoryMonitor` | 20 | neuer Snapshot/`close()` |
| Diagnoseberichte/Thread-Dumps | Diagnosewriter | je 500 Dateien | älteste Datei nach Neuschreiben löschen |
| Coverbilder | `MainWindow` | 2, eines je Deck | Ersetzen, Leeren, Fenster-Dispose |
| Katalog-/Queue-Tooltips | jeweilige RowView | an begrenzten Row-Pool gebunden | `dispose()` |
| Logdateien | Logging-Konfiguration | 5 MiB × 4 Dateien | Rotation |
| VLC-Player | jeweiliges AudioBackend | Decks + höchstens eine Vorschau | `close()`/native `release()` |

Die internen Warteschlangen des Standard-`ThreadPoolExecutor` werden nicht direkt
verwendet. `BoundedThreadPoolExecutor` reserviert vor jeder Einreichung einen Platz und
weist weitere Arbeit bei erreichter Kapazität zurück. Dadurch kann keine Liste von
Cover-, Preview- oder Persistenzaufträgen unbegrenzt anwachsen.

## VLC und vorbereitete Medien

Ein `VlcAudioBackend` besitzt genau einen Player und höchstens eine übernommene
Media-Referenz. Beim Ersetzen wird die vorherige Media-Referenz explizit freigegeben.
Ein vom Preload vorbereiteter, wegen Generation oder Zustandsänderung nicht übernommener
Handle wird über `DeckController.discard_prepared()` freigegeben.

`close()` ist idempotent. Es stoppt den Volume-Worker, wartet begrenzt auf dessen Ende,
stoppt und released den Player, released das Medium und entfernt die Pythonreferenzen.
Ein Klassenzähler liefert die Zahl noch aktiver VLC-Player einschließlich Vorschau.

## GUI-Ressourcen

RowViews besitzen ihre Widgets und Tooltips. Ihr `dispose()` beendet verzögerte
Tooltip-Callbacks und zerstört den Widget-Unterbaum. `MainWindow` hält höchstens ein
`CTkImage` je Deck und entfernt beim Leeren zusätzlich die native Label-Bildreferenz.
Beim Fensterschließen werden sämtliche noch registrierten `after()`-IDs abgebrochen,
Tooltips und RowViews freigegeben und Coverreferenzen geleert, bevor Tk zerstört wird.

## Cue-Vorschau

Der Vorschaucontroller verwendet einen seriellen, begrenzten Executor. Ein lokales
Stop-Event gehört genau zu einer Vorschaugeneration; eine neue Generation kann das
Event der alten nicht versehentlich überschreiben. Der Backend-Handle wird im
`finally`-Pfad geschlossen. `close()` signalisiert Abbruch und beendet den Executor
geordnet.

## MemoryMonitor

Im Diagnosebetrieb erfasst der Controller alle fünf Sekunden:

```text
process_rss_bytes
python_traced_bytes
python_peak_bytes
active_thread_count
gui_event_queue_size
active_worker_count
cover_cache_size
registered_widget_count
active_preview_count
active_vlc_player_count
```

RSS wird unter Windows über `GetProcessMemoryInfo` ermittelt und enthält damit auch
native VLC-, Tk-, PIL- und Decoderallokationen. `tracemalloc` wird nicht beim normalen
Programmstart aktiviert, sondern erst mit **Test starten/reset**. Der Bericht vergleicht
den Startsnapshot mit dem Snapshot beim Testende und listet höchstens 20 positive
Zuwächse. Wächst RSS deutlich stärker als der Python-Heap, sind zuerst VLC, Tk,
CustomTkinter, PIL und native Audiopuffer zu prüfen.

## Datenbankobjekte

Repositoryzugriffe besitzen ihre Verbindung nur innerhalb eines `with`-Blocks. Cursor
und Transaktion verlassen damit nach jeder Operation ihren definierten Gültigkeitsbereich.
Der serielle Persistenzexecutor hält keine offenen Verbindungen zwischen Aufträgen.
