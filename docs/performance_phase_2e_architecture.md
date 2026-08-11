# PartyPlayer – Performance-Stabilisierung Phase 2E

## Ziel und Grenze

Phase 2E reduziert den CustomTkinter-Layout- und Draw-Aufwand beim Aufbau und bei
Änderungen der Oberfläche. Playback, Crossfade, VLC-Decks, Datenbank und
Prozessarchitektur bleiben unverändert.

## Bestandsaufnahme

Der Anwendungscode enthält keinen wiederkehrenden Aufruf von `update()` oder
`update_idletasks()`. Ein einmaliger `update_idletasks()`-Aufruf bleibt in einem kleinen
modalen Dialog bestehen, weil erst danach dessen angeforderte Größe für die Zentrierung
bekannt ist. Er gehört weder zum Katalog-/Queue-Rendering noch zu einer Schleife.

Der wesentliche eigene Verstärker war die rekursive Fokusaufbereitung kompletter
Katalog- und Queue-Widgetbäume nach Renderläufen. Außerdem baute der Katalog trotz
nur etwa 15 sichtbarer Einträge einen Pool von bis zu 50 CustomTkinter-Zeilen auf.

## Begrenzter Katalogpool

Der initiale Pool umfasst höchstens 18 Zeilen: die ungefähr 15 sichtbaren Zeilen plus
eine kleine Reserve. Erst ein Benutzerscrollen erweitert den Pool in Achterblöcken.
Seitenwechsel verwenden bestehende Zeilen und Tooltips wieder. Dies reduziert den
Startaufwand von 50 Zeilen mit jeweils Label, Frame, vier Schaltflächen und vier
Tooltips auf den tatsächlich benötigten sichtbaren Bereich.

Eine vollständige Canvas-Virtualisierung bleibt eine mögliche spätere Stufe. Sie wird
erst umgesetzt, wenn reale Phase-2E-Messwerte trotz des begrenzten Pools weiterhin
kritische Layoutzeiten zeigen. Damit bleibt die derzeit bewährte Bedien- und
Callbacklogik unangetastet.

## Zusammengefasste GUI-Arbeit

Katalog und Queue melden Layoutänderungen an je einen zusammengefassten Callback.
Mehrere Anforderungen vor dessen Ausführung erhöhen zwar den Diagnosezähler, erzeugen
aber keinen weiteren Callback. Fokusbindungen werden gesammelt, bis keine Renderchunks
und Layoutaktualisierungen mehr ausstehen. Dabei wird nur der Widget-Unterbaum einer
neu erzeugten Zeile besucht; bereits vorbereitete Widgets werden nicht erneut gebunden.

OptionMenus besitzen einen Cache aus Werteliste und Auswahl. `configure(values=...)`
und `set(...)` werden getrennt nur bei einer tatsächlichen Änderung ausgeführt.

## Tooltips

Tooltips bleiben stabile Bestandteile der wiederverwendbaren Zeilen. Durch den
begrenzten Anfangspool entstehen beim Start nur Tooltips für tatsächlich vorbereitete
Zeilen. Ein zentraler Tooltipmanager würde zusätzliche Zustands- und Ereignislogik
einführen und wird deshalb nur dann prototypisiert, wenn Messwerte nach Phase 2E noch
einen relevanten Tooltipanteil belegen.

## Diagnose

Der Diagnosebericht enthält Anforderungen und Ausführungen der Katalog-/Queue-
Layoutaktualisierung, Fokusanforderungen/-ausführungen, Scrollpositionsänderungen und
OptionMenu-Konfigurationen/-Auswahlen. Gemessen werden:

```text
gui.layout.catalog_refresh
gui.layout.queue_refresh
gui.layout.focus_apply
gui.layout.scroll_apply
gui.layout.optionmenu_update
```

Der unabhängige Watchdog liest ausschließlich thread-sicher veröffentlichte skalare
Werte. Dumps enthalten die Zeit seit dem letzten abgeschlossenen Callback, ausstehende
Layout-/Fokusarbeit, ausstehende Katalog-/Queue-Zeilen und die Anzahl erzeugter Zeilen.
Er greift niemals auf Tk-Widgets zu.

## Manuelle Abnahme

Die technische Umsetzung und automatisierten Tests ersetzen keinen realen Stabilitätstest.
Zu prüfen sind insbesondere ein Katalog mit mindestens 1000 Titeln, schnelles Scrollen,
Seitenwechsel, Queueänderungen, Mixer ein-/ausblenden, Vollbildwechsel und parallele
Wiedergabe. Phase 2F wird ausschließlich aus den dabei gemessenen häufigsten und
größten Verzögerungen abgeleitet.
