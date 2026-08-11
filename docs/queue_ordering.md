# Deterministische Queue-Reihenfolge

PartyPlayer sortiert Queue-Einträge nach vier stabilen Schlüsseln:

1. Priorität absteigend;
2. manuelle Position aufsteigend;
3. Einfügezeit aufsteigend;
4. Queue-ID aufsteigend.

Eine höhere Priorität wird damit zuerst berücksichtigt. Innerhalb derselben
Prioritätsstufe bleibt die manuell festgelegte Position maßgeblich. Einfügezeit
und ID lösen Gleichstände deterministisch auf, auch nach einem Neustart.

Beim Kopieren einer offenen Queue in eine neue Session wird genau dieselbe
Reihenfolge verwendet und in zusammenhängende neue Positionen überführt.
