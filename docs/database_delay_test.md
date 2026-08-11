# PartyPlayer – belastbarer Datenbankverzögerungstest

## Zweck

Die Auswahl `database_delay` benennt nicht mehr nur einen Bericht. Ein explizit
gestartetes Szenario erzeugt einen isolierten Messzeitraum und verzögert ausschließlich
History- und Queuepersistenz. In-Memory-Queue, Decks, Fade-Rampe, GUI, Preload und Cover
werden nicht künstlich verlangsamt.

## Bedienung

1. Automatik starten und ausreichend Titel für einen vollständigen Übergang vorhalten.
2. Im Mixer das Szenario **Datenbankverzögerung** auswählen.
3. Im Millisekundenfeld den Testwert eingeben; Standard ist `1000`.
4. **Test starten/reset** wählen. Dadurch werden Performance-, Heartbeat-, Dispatcher-
   und abgeschlossene Workerstatistiken zurückgesetzt.
5. Mindestens einen vollständigen automatischen Crossfade abwarten.
6. Prüfen, dass das eingehende Deck ohne Unterbrechung weiterläuft.
7. **Test beenden + Bericht** wählen.

Der letzte Schritt wartet in einem Hintergrundworker auf alle bereits eingereihten
Persistenzaufträge. Die Oberfläche bleibt bedienbar. Nach Abschluss erscheint wie
gewohnt der Speicherort des Berichts; zugleich wird die künstliche Verzögerung
deaktiviert.

## Injektionsgrenze

Die Verzögerung wird nur ausgeführt, wenn Performance-Diagnostik aktiv ist und das
Szenario explizit gestartet wurde. Sie liegt unmittelbar vor dem jeweiligen
Repositoryaufruf im `playback-persist-worker`:

```text
History-Job: database.injected_delay → database.history.commit
Queue-Job:   database.injected_delay → database.queue.commit
```

Produktionsmodus kann kein Szenario aktivieren. Nach Szenarioende liefert der
Injektionspunkt ohne Wartezeit zurück.

## Bericht und Gültigkeit

Der Abschnitt `Scenario` enthält Name, Start-/Endzeit, Verzögerung, Resetstatus,
Übergangszahl und Persistenzzähler. Für `database_delay` wird zusätzlich ausgegeben:

```text
acceptance_data_present: true|false
```

`true` ist nur möglich, wenn nach dem Reset mindestens ein Transition-Abschluss und
mindestens ein Persistenzauftrag erfasst wurden. Die bloße Kontextauswahl ergibt
`false` und ist damit kein gültiger Lastnachweis.

Die relevanten Messpunkte sind:

```text
database.injected_delay
database.history.total
database.history.commit
database.queue.total
database.queue.commit
transition_completion.enqueue_history
transition_completion.enqueue_queue_persist
transition_completion.total
worker.history_persist
worker.queue_persist
worker.playback_persist
```

## Negativkontrolle

Ein automatisierter Kontrolltest ruft denselben Injektionspunkt synchron auf und
bestätigt, dass die aufrufende Ausführung entsprechend lange blockiert. Damit ist
belegt, dass der Testmechanismus eine alte synchrone Implementierung erkennen würde.

## Erwartete reale Abnahme

Bei 1000 ms Verzögerung soll `transition_completion.total` typisch unter 15 ms und
maximal unter 50 ms bleiben. `worker.playback_persist` muss dagegen mindestens ungefähr
1000 ms je verzögertem Auftrag ausweisen. Der Heartbeat soll kein kritisches Ereignis
enthalten und das eingehende Deck muss durchgehend spielen.
