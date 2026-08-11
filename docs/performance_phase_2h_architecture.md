# Performance-Stabilisierung Phase 2H

## Queue-Virtualisierung

Die logische Queue bleibt vollständig im Modell. Die Oberfläche hält nur einen aus
der sichtbaren Höhe bestimmten Pool von 10 bis höchstens 20 `QueueRowView`-Objekten.
Vier zusätzliche Zeilen dienen als Overscan. Mausradbewegungen verschieben den
logischen Startindex; anschließend werden die vorhandenen Zeilen feldweise neu
gebunden. Die Queuegröße beeinflusst damit die Zahl der Tk-Widgets nicht mehr.

Nach dem ersten Poolaufbau erzeugen Abspielen, Verschieben, Ergänzen, Entfernen und
Sortieren keine weiteren Zeilen. Beim Leeren bleibt der kleine Pool kontrolliert zur
Wiederverwendung erhalten. Fokusanforderungen und das Zurücksetzen der physischen
Canvasposition werden je Renderdurchlauf zusammengefasst.

## Gemeinsame Tooltips

Alle Aktionsfelder der virtuellen Queuezeilen sind bei einem
`SharedTooltipManager` registriert. Er besitzt unabhängig von der Zielanzahl
höchstens ein sichtbares Tooltipfenster. Beim Rebinding wird der Text am vorhandenen
Registrierungshandle geändert.

## Callback-Messungen

Jeder Callback-Aufruf verwendet weiterhin einen lokalen Startwert. Verschachtelte
Aufrufe besitzen getrennte Messkontexte. Während eines Diagnoseszenarios werden
unendliche, negative und die bisherige Szenariodauer überschreitende Zeitwerte als
ungültig markiert und nicht aggregiert. Der Bericht nennt Grund und Zähler.

## Memory-Stress

Im Szenario `memory_stress` wird jeder Übergang von einer gefüllten zu einer leeren
Queue nach `gc.collect()` als Zyklus erfasst. Die begrenzte Historie enthält
Queue-Peak, RowViews, Tk-Widgets, Tooltips, Python-Heap, RSS sowie erzeugte und
zerstörte Widgets. Für die Abnahme sind fünf manuelle Zyklen mit je 100 Einträgen
vorgesehen; nach dem Aufwärmzyklus darf kein monotoner Daueranstieg auftreten.
