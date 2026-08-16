# Equalizer und wahrgenommene Lautstärke

DeckRelay wendet den Equalizer für jedes Deck unabhängig an. Die Auswahl eines
Presets verändert weder den sichtbaren Deckregler noch Masterlautstärke oder
Crossfaderposition.

## Warum ein Preset leiser klingen kann

Die Presets `Rock`, `Pop`, `Bluesrock` und `Dance` heben einzelne Frequenzbereiche
an. Gleichzeitig verwendet DeckRelay einen Preamp von `-3 dB`, um durch diese
Anhebungen verursachtes Clipping zu vermeiden. Deshalb kann ein Titel nach dem
Wechsel von `Aus` auf ein Preset hörbar leiser wirken, obwohl sämtliche
Lautstärkeregler unverändert bleiben.

Wie stark der Unterschied wahrgenommen wird, hängt vom Titel und dessen
Frequenzverteilung ab. Angehobene Bänder gleichen die Absenkung nur teilweise aus.
`Neutral` verwendet einen aktiven Equalizer mit Bändern und Preamp bei `0 dB`.
`Aus` deaktiviert den VLC-Equalizer vollständig.

## Equalizer und Lautheitsnormalisierung

Equalizer und Lautheitsnormalisierung sind getrennte Verarbeitungsstufen. Eine
ReplayGain-, Analyse- oder manuelle Lautheitskorrektur verstellt beim EQ-Wechsel
nicht automatisch den Sicherheits-Preamp. Eine zusätzliche automatische
Lautheitskompensation erfolgt nicht, weil sie ohne verlässlichen Laufzeit-Limiter
erneut Übersteuerungen verursachen könnte.

Beim Vergleichen von Presets daher nicht nur auf die Stellung der Regler achten,
sondern die tatsächliche Wiedergabe abhören. Ein Lautstärkeunterschied beim
Aktivieren der mitgelieferten Presets ist grundsätzlich erwartbar.
