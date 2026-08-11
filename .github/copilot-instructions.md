# Copilot Instructions — Party Player

Diese Datei basiert auf den verbindlichen Vorgaben des MP3-Manager-Projekts.

- Technologie: Python 3.12+, CustomTkinter, tkinter/ttk, SQLite, pygame, TinyTag, Pillow.
- Architektur strikt einhalten: UI → Controller → Service → Repository → SQLite.
- UI enthält weder SQL noch Geschäftslogik oder Dateimanipulationen.
- Wiedergabe ausschließlich über eine `MusicPlayer`-Abstraktion; UI greift nie direkt auf pygame zu.
- Musikdateien sind schreibgeschützt zu behandeln: nicht automatisch umbenennen, verschieben, löschen oder Metadaten überschreiben.
- Datenbankänderungen erfolgen über Migrationen, SQL ist parametrisiert und `SELECT *` ist verboten.
- Auf mindestens 100.000 Titel auslegen: Pagination, Indizes, Lazy Loading und Hintergrundarbeit verwenden.
- Tkinter nur im Hauptthread aktualisieren; Worker kommunizieren über Queue, Callbacks oder `after()`.
- Projekt-Logging verwenden, niemals `print()`.
- Code und Bezeichner Englisch, UI-Texte Deutsch; Type Hints und Docstrings sind Pflicht.
- Tests verwenden temporäre SQLite-Datenbanken, gemockte Player und keine echte Musiksammlung.
- Party-Oberflächen zeigen keine Tag-Editoren, Datenbankwartung oder Dateioperationen.

