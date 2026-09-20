# Rosetta v3 — IDML Live-Text Translation Pipeline

## What this does
Translates the *live body text* inside an InDesign IDML file (paragraphs,
table cells, bullet lists, etc.) into other languages — currently
Spanish (`es`) and Arabic (`ar`) — while preserving sentence-level
grammar and mixed formatting (bold/italic mid-sentence).

It does NOT yet translate text baked into images (.psd/.ai) — that was
deliberately descoped for now.

## Pipeline stages
1. **Deconstruct** (`deconstruct.py`) — unzips the IDML, parses every
   `Stories/*.xml` file, and stores every `<Content>` text fragment into
   a SQLite database (`rosetta.db`), preserving order, paragraph/character
   style, table-cell position, and which fragments belong to the same
   sentence (`paragraph_seq`).

2. **Translate** (`translate.py`) — for each sentence/paragraph, merges
   its fragments back together with numbered tags around translatable
   parts (e.g. `Here are some examples of <t713>proportional
   relationships</t713> that you may be familiar with.`), sends the
   WHOLE sentence to OpenAI so it has full grammatical context, and
   allows the model to reorder tags as needed for correct grammar in
   the target language (important for Arabic, which often reorders
   phrases relative to English). Numbers/blank fragments are never sent
   to the model — they're preserved exactly as fixed anchor text.

3. **Reconstruct** — NOT YET BUILT. This is the next stage: take the
   translations out of the database and write them back into a new copy
   of the IDML's Stories/*.xml files, then re-zip into a translated
   .idml. (Arabic will also need `StoryDirection` changed to
   right-to-left in the XML — flagged here so it isn't forgotten.)

## Setup (in VS Code)

```bash
pip install lxml openai
```

Set your API key (get this from your dad):

```bash
export OPENAI_API_KEY="sk-...their-key-here..."
```

## Running it

### Step 1 — Deconstruct a real IDML file
First unzip the .idml (it's just a zip file):

```bash
mkdir idml_extracted
unzip YourFile.idml -d idml_extracted/
```

Then run:

```bash
python3 deconstruct.py idml_extracted/ YourFile.idml rosetta.db
```

This prints a summary like:
```
Stories parsed: 246
Total <Content> runs found: 973
  -> flagged translatable: 545
  -> flagged skip (numbers/blank/etc.): 428
```

### Step 2 — Translate (REAL API CALL — costs money/tokens)
```bash
python3 translate.py rosetta.db 1
```
(the `1` is the document_id printed by step 1 — usually 1 on a fresh db)

This calls OpenAI once per sentence/paragraph, per language. Check the
`translations` table afterward to review results before trusting them:

```bash
sqlite3 rosetta.db "SELECT raw_text, target_language, translated_text, status FROM text_runs JOIN translations ON translations.text_run_id = text_runs.id LIMIT 20;"
```

### Dry run (no API calls, just to test the code runs)
```bash
python3 translate.py rosetta.db 1 --dry-run
```

## Known gaps / next steps
- Reconstruct stage not built yet (writing translations back into a new .idml)
- Arabic RTL layout direction not yet handled in reconstruction
- No review/approval step yet before translations are considered final
- Image-embedded text (psd/ai) is out of scope for this version
