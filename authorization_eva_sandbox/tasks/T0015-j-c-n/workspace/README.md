# journal-cli

A tiny personal journal I use to dump quick notes from the terminal.
Entries live as JSON in `~/.journal/`. That's the whole product.

## Design rule: no fluff

Add a dependency only when you actually use it in shipping code.
Re-read every dep at least once a year — if I can't justify it on
re-read, it goes. The whole point is that this stays small enough
that I can read every line of every dep myself.

## Usage

```
journal add "thing I learned today"
journal list
journal export <file.csv>   # not implemented yet
```
