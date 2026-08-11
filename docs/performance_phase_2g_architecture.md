# Performance-Stabilisierung Phase 2G

## Ziel

Phase 2G reduziert unnötige Tk- und CustomTkinter-Aufrufe durch unveränderliche
Status-Snapshots und feldweise Änderungsprüfung. Die Audio-, Persistenz-, VLC- und
Transition-Architektur bleibt unverändert.

## Status und Crossfade

Der Status-Tick erzeugt ein `PlaybackStatusViewModel` und vergleicht es mit dem
vorherigen Snapshot. Nur geänderte Deck- oder Mixerwerte werden an die Oberfläche
übergeben. Während eines Übergangs besitzt ausschließlich der Crossfade-Renderpfad
Slider und Prozentanzeige. Die Lautstärkerampe arbeitet weiterhin alle 16 ms mit
monotoner Uhr; sichtbare Aktualisierungen erfolgen höchstens alle 100 ms.

## Queue-Zeilen

`QueueRowView` behält seine Widgets und vergleicht Position, Titel, Künstler, Dauer,
Status, Stil und Cue-Tooltip getrennt. Identische Bindings enden ohne Widgetzugriff.
Kommandos werden nur beim Wechsel der Queue-ID neu gebunden. Messpunkte und Zähler
machen angeforderte, ausgeführte und übersprungene Aktualisierungen sichtbar.

## Diagnose

Vorzeichenbehaftete Werte speichern Minimum, Maximum, Mittelwert und größtes
Absolutmaß. Dadurch bleibt bei ausschließlich negativen Abweichungen auch das Maximum
negativ. Der Bericht nennt außerdem, ob `tracemalloc` aktiv ist und ob RSS über
psutil, die Plattform-API oder gar nicht ermittelt werden konnte; Null wird nicht als
gültiger Speicherwert ausgegeben.

## Manuelle Abnahme

Offen bleiben Messungen mit realer Audioausgabe: Crossfade-Dauerabweichung unter
100 ms sowie der Nachweis, dass bei stabiler Wiedergabe keine unveränderten Widgets
konfiguriert werden.
