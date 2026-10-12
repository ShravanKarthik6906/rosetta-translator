# Rosetta — IDML Live-Text Translation Pipeline

Rosetta translates the live text inside an Adobe InDesign `.idml` file
(paragraphs, table cells, bullet lists) into another language, while
preserving mixed formatting, fixing the layout problems translation
introduces, and flagging anything it can't safely resolve on its own for a
human to review. It ships as both a set of CLI pipeline stages and a small
Flask web app.

Currently supports Spanish (`es`) and Arabic (`ar`) as target languages.

## Why this is harder than "call an API and swap the text"

- **Mixed formatting mid-sentence.** A single sentence can span several
  `<Content>` XML fragments with different bold/italic runs. Rosetta
  reassembles the full sentence with numbered tags around each fragment so
  the model has real grammatical context, then maps the translation back
  onto the original fragment boundaries.
- **Arabic is right-to-left.** Reconstruction flips every `StoryDirection`
  attribute, applies a bundled Arabic-capable font (the document's own fonts
  have no Arabic glyphs and would render as tofu otherwise), and rewrites
  the document's font metadata to match.
- **Translated text doesn't fit the original frame.** Spanish and Arabic
  text is routinely longer than the English original. Rosetta measures
  real text width using actual font metrics and HarfBuzz text shaping
  (not a character-count guess - this matters especially for Arabic's
  contextual letterforms) against the real frame geometry parsed from the
  IDML's own `Spreads` XML, then resolves overflow in tiers: try wrapping
  onto another line, then a controlled font-size reduction, then a real
  API call asking for a shorter phrasing that preserves meaning - and if
  none of that genuinely fits, it flags the fragment instead of silently
  producing broken layout.
- **The document's real fonts usually aren't available on the machine
  running the pipeline.** Licensed commercial fonts (e.g. Myriad Pro, Museo
  Sans) are substituted with metric-reasonable, open-licensed equivalents
  (Liberation Sans, Inter) bundled in `fonts/`, so measurement works
  identically on any machine, including on a cloud host.
- **Translation can silently go wrong.** A QA pass cross-checks for
  inconsistent translations of repeated text, missing translations, and
  layout-risk heuristics, and produces a plain-English HTML report plus a
  machine-readable JSON one per language.

## Architecture

```
  input: .idml + Links/ folder, zipped
         |
         v
  [ Flask web app (app.py) ] -- uploads the zip, runs the pipeline below
         |                      in a background thread, reports progress
         v
  1. Deconstruct          (deconstruct.py)
     unzip .idml -> parse every Stories/*.xml -> store each text
     fragment + its style into SQLite (rosetta.db)
         |
         v
  2. Story placements     (story_placements.py)
     map each story to the page/frame it's actually placed on,
     by parsing the IDML's Spreads XML
         |
         v
  3. Translate            (translate.py)                [real OpenAI calls]
     rebuild each full sentence with tagged fragments, translate with
     full grammatical context, map the result back onto fragments
         |
         v
  3b. Overflow check      (overflow.py, overflow_resolve.py)
      measure real fit (HarfBuzz shaping + real frame geometry)
        -> wrap onto another line
        -> controlled font-size shrink
        -> retranslate for a shorter phrasing     [real OpenAI call]
        -> flag for human review if still too long
         |
         v
  4. Reconstruct          (reconstruct.py)
     write translations back into a fresh copy of the IDML, flip
     StoryDirection + apply Arabic font for RTL, rewrite linked asset
     paths, re-zip into a valid .idml + delivery bundle
         |
         v
  5. Exception checks     (exception_checks.py)
     cross-check inconsistent/missing translations + layout-risk
     heuristics -> HTML + JSON QA report per language
         |
         v
  output: translated .idml + delivery bundle + QA report
```

Every stage reads/writes the same SQLite database (`rosetta.db`), so each
one can also be run and inspected independently from the CLI.

## Setup

Developed and tested on Python 3.13.7.

```bash
git clone https://github.com/ShravanKarthik6906/rosetta-translator.git
cd rosetta-translator
python3 -m venv .venv
source .venv/bin/activate        # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
# then edit .env and set OPENAI_API_KEY to a real key
```

## Running it

### Option A — the web app

```bash
python app.py
```

Open `http://127.0.0.1:5050`. Upload a `.zip` containing exactly one
`.idml` file and a `Links` folder alongside it, pick target language(s),
and watch the pipeline run with live progress. When it finishes you can
download the translated delivery bundle and the exception/QA report for
each language.

Every upload makes real, billed OpenAI API calls. Set `SITE_PASSWORD` in
`.env` before exposing this publicly.

### Option B — CLI, stage by stage

Useful for debugging a single stage or scoping a test to a few paragraphs
without spending on a full real API run.

```bash
# unzip the .idml (it's just a zip file) alongside its Links/ folder first
mkdir idml_extracted && unzip YourFile.idml -d idml_extracted/

python3 deconstruct.py idml_extracted/ YourFile.idml rosetta.db
python3 story_placements.py rosetta.db 1 idml_extracted/
python3 translate.py rosetta.db 1 --dry-run        # no API calls, sanity check
python3 translate.py rosetta.db 1 --langs=es,ar    # real, billed API calls
python3 overflow_resolve.py rosetta.db 1 idml_extracted/ es,ar
python3 reconstruct.py rosetta.db 1 es idml_extracted/ idml_work_copy_es
python3 exception_checks.py rosetta.db 1 es,ar
```

`translate.py` also supports `--limit=N`, `--story-id=N`, and
`--paragraph-seq=N` to scope a run tightly while testing, since re-running
it is a real, non-deterministic, billed API call each time.

## Known limitations

This is an honest list, not a disclaimer - please read it before relying on
this for anything important:

- Tested end-to-end on one real document, in two target languages (Spanish
  and Arabic). It has not been validated against a broad range of InDesign
  documents, and it does not claim to handle arbitrary IDML files out of
  the box.
- A small number of overflow cases cannot be resolved automatically (fit
  check still fails after wrapping, shrinking, and a retranslate-for-brevity
  attempt) and are flagged in the exception report for manual frame
  resizing rather than silently left broken.
- Overflow measurement for Spanish (and any other Latin-script target)
  uses substitute fonts (Liberation Sans / Inter), not the document's
  actual licensed fonts, since those usually aren't available on the
  machine running the pipeline. This is metric-reasonable but not
  pixel-exact.
- Page/frame mapping (`story_placements.py`) only covers stories that are
  placed directly on a spread; stories that live only on a master page
  (e.g. running headers/footers that repeat per page) aren't currently
  mapped to a specific frame.
- Any fragment resolved via tier-4 "retranslate for brevity" changes actual
  wording, not just layout, so it's always routed to the exception report
  for human review rather than treated as final.
- Translations are machine-generated and have not been reviewed by native
  speakers. The QA exception report checks consistency and completeness
  (repeated text translated the same way, nothing missing, layout risk) -
  it does not check linguistic correctness.
- Document text is sent to OpenAI's API for translation. Don't upload
  confidential documents unless that's acceptable for your use case.
- There is no automated test suite yet.

## Pipeline stages in detail

1. **Deconstruct** (`deconstruct.py`) — unzips the IDML, parses every
   `Stories/*.xml` file, and stores every `<Content>` text fragment into
   SQLite, preserving order, paragraph/character style, table-cell
   position, and which fragments belong to the same sentence
   (`paragraph_seq`).
2. **Story placements** (`story_placements.py`) — maps each story to the
   page/frame it's actually placed in, by parsing the IDML's `Spreads`
   XML, so overflow checking later knows the real frame geometry for each
   fragment.
3. **Translate** (`translate.py`) — rebuilds each full sentence with
   numbered tags around translatable fragments and sends it to OpenAI with
   full grammatical context, letting the model reorder tags where the
   target language's grammar requires it (important for Arabic). Numbers
   and blank fragments are never sent to the model - they're preserved
   exactly as fixed anchor text.
4. **Overflow check** (`overflow.py` / `overflow_resolve.py`) — for every
   translated fragment, measures whether it actually fits its real frame
   using HarfBuzz text shaping over real font metrics, then resolves
   overflow in tiers: wrap onto another line → controlled font-size
   reduction → a real API call for a shorter phrasing → flag for human
   review if none of that genuinely fits.
5. **Reconstruct** (`reconstruct.py`) — writes translations back into a
   fresh copy of the IDML's `Stories/*.xml`, flips `StoryDirection` to RTL
   and applies the bundled Arabic font for Arabic output, rewrites linked
   asset paths to be portable across machines, and re-zips into a valid
   `.idml` plus a delivery bundle.
6. **Exception checks** (`exception_checks.py`) — cross-checks for
   inconsistent translations of repeated text, missing translations, and
   layout-risk heuristics, producing a plain-English HTML report and a
   machine-readable JSON one per language.

`app.py` is a thin Flask wrapper around all of the above - it doesn't
reimplement any pipeline logic, just runs the existing functions in a
background thread and reports progress.
