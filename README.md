# Viking2MFP

Copies **Kuchnia Vikinga** ("box diet") meals into the **MyFitnessPal** food diary.

For every delivered day in a date range it reads each meal's calories, carbohydrate,
fat and protein from Kuchnia Vikinga and writes one MyFitnessPal *quick add* diary
entry per meal into the matching meal slot.

```
viking2mfp --from 2026-10-14 --to 2026-10-20
```

## AI Disclosure

Developed entirely by Deepseek 4.1 Flash under Oh My Pi harness. Manually tested up to 'works with my accounts' standard

## Install

```
pipx install viking2mfp                                       # once published on PyPI
pipx install .                                                # from a checkout
pipx install "git+https://github.com/keton/viking2mfp"
```

`pipx` puts `viking2mfp` on your PATH in its own virtualenv. Upgrades and removal are
`pipx upgrade viking2mfp` / `pipx uninstall viking2mfp`.

Optional extra, only needed if you log in with a Chromium-family browser (Chrome,
Brave, Edge, Vivaldi) **and** want the script to read its cookie database, whose
values are AES-encrypted:

```
pipx install "viking2mfp[chromium]"
pipx inject viking2mfp cryptography secretstorage      # for an existing install
```

Firefox-family browsers (Firefox, Zen, LibreWolf) store cookie values as plain text
and need neither.

Without pipx, `pip install .` also works, or run it straight from a checkout with
`python -m viking2mfp`. It needs Python 3.9 or newer (written for 3.9+, developed and
tested on 3.14) and `requests`, which is declared as a dependency.

## Usage

| Command | Effect |
|---|---|
| `viking2mfp --from 2026-10-14 --to 2026-10-20` | copy that window, skipping anything already logged |
| `viking2mfp --from 2026-10-01` | copy from that date up to the last delivery Kuchnia Vikinga knows about |
| `viking2mfp --from 2026-10-14 --dry-run` | print the table, write nothing |
| `viking2mfp --login-only` | just refresh the two sessions |
| `viking2mfp --from 2026-10-01 --force` | add everything, even where the diary is already filled |

Without `--yes` the script prints the full plan (date, KV meal, target MFP meal,
macros, dish name, totals) and asks before writing anything.

### Running it twice is safe

The diary itself is the record of what has been copied, so there is no local
bookkeeping to keep in step and nothing to remember between runs. Before adding
anything the script reads each day it is about to touch, and **a meal is skipped
when its diary slot for that day already holds an entry** — regardless of what that
entry is, or which machine or run put it there:

```
already in the diary, skipped: 25 meal(s) (use --force to add them anyway)
  2026-10-14  Śniadanie (571 kcal), II śniadanie (317 kcal), Obiad (557 kcal), …
  2026-10-15  Śniadanie (533 kcal), …
```

So `--from 2026-10-14 --to 2026-10-28` fills the gaps and leaves the days you have
already dealt with alone. `--force` overrides the check and adds everything anyway.

If a day's diary cannot be read, the script stops with an error rather than assume
nothing is logged — guessing there would duplicate entries, so `--force` is the
only way past it.
