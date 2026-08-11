# DeckRelay – Messergebnisse und Priorisierung für Phase 2

Zugehörige Testprotokolle:

Zugehörige Diagnoseberichte:

## 1. Beobachtete GUI-Verzögerungen

Zeitpunkt, Testkontext, reproduzierbare Aktion und beobachtete Auswirkung dokumentieren.

## 2. Heartbeat-Auswertung

- maximale Verzögerung:
- durchschnittliche Verzögerung:
- Warnungen:
- kritische Verzögerungen:
- erkennbare Verzögerungsserien:

## 3. Langsamste Status-Tick-Teilschritte

| Messpunkt | Anzahl | Durchschnitt ms | Maximum ms | Langsame Aufrufe |
|---|---:|---:|---:|---:|
| `status_tick.total` | | | | |
| `status_tick.background_callbacks` | | | | |
| `status_tick.deck_a_status` | | | | |
| `status_tick.deck_b_status` | | | | |
| `status_tick.crossfader` | | | | |
| `status_tick.queue_state` | | | | |
| `status_tick.queue_statistics` | | | | |
| `status_tick.autoload` | | | | |
| `status_tick.render` | | | | |

## 4. Langsamste sonstige GUI-Callbacks

Queue-, Katalog-, Cover- und Tooltip-Messwerte gegenüberstellen.

## 5. Dispatcherverhalten

- maximale Queuegröße:
- Kapazitätsauslastung:
- veröffentlichte/verarbeitete Ereignisse:
- zusammengeführte/verworfen Ereignisse:
- kritische Überläufe:
- maximale und durchschnittliche Dispatchdauer:

## 6. Workeranzahl und Laufzeiten

Preload, Cover, Cue-Preview und Verzeichnisimport getrennt auswerten. Ungewöhnlich lange
Worker nur als Beobachtung kennzeichnen, solange ihre Ursache nicht gemessen ist.

## 7. NAS-Einflüsse

Nur Messwerte und reproduzierbare zeitliche Zusammenhänge eintragen.

## 8. SQLite-Einflüsse

Nur Messwerte und reproduzierbare zeitliche Zusammenhänge eintragen.

## 9. VLC-Einflüsse

Nur Messwerte und reproduzierbare zeitliche Zusammenhänge eintragen.

## 10. Empfohlene Phase-2-Maßnahmen

Maßnahmen nach gemessener Häufigkeit, maximaler Blockadedauer und Auswirkung priorisieren.

### Entscheidungshilfe

- Dominieren VLC-/Datenbankteile von `_status_tick()`: Status-Snapshots außerhalb des
  GUI-Threads priorisieren.
- Dominiert `gui.queue_render`: inkrementelles oder virtualisiertes Queue-Rendering
  priorisieren.
- Dominieren Worker-Rückstaus: begrenzte Executor- und Prioritätsstruktur priorisieren.
- Keine grundlegende Architekturänderung ohne reproduzierbare Messgrundlage beschließen.
