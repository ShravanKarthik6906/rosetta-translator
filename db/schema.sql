-- Rosetta v3 schema: IDML live-text translation pipeline

CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    source_idml_filename TEXT NOT NULL,
    processed_at TEXT DEFAULT (datetime('now'))
);

-- One row per Stories/*.xml file
CREATE TABLE stories (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    story_self_id TEXT NOT NULL,      -- e.g. "u88c" (from filename Story_u88c.xml)
    source_file TEXT NOT NULL,        -- relative path within idml package
    UNIQUE(document_id, story_self_id)
);

-- One row per <Content> element found anywhere in a story
CREATE TABLE text_runs (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL REFERENCES stories(id),
    sequence_index INTEGER NOT NULL,       -- order of this Content within the story (for reconstruction)
    paragraph_style TEXT,                  -- e.g. "_Family Letter:FL body"
    character_style TEXT,                  -- e.g. "myriad bold"
    raw_text TEXT NOT NULL,                -- exact original text, incl. whitespace
    is_table_cell INTEGER DEFAULT 0,       -- 1 if this Content is inside a <Cell>
    table_self_id TEXT,                    -- which table, if applicable
    cell_name TEXT,                        -- e.g. "0:0" row:col, if applicable
    needs_translation INTEGER DEFAULT 1,   -- 0 = skip (pure numbers, punctuation-only, etc.)
    paragraph_seq INTEGER NOT NULL,        -- groups fragments belonging to the same paragraph occurrence
    context_note TEXT                      -- optional extra context (e.g. nearby alt-text) to aid translation
);

-- One row per (text_run, target_language)
CREATE TABLE translations (
    id INTEGER PRIMARY KEY,
    text_run_id INTEGER NOT NULL REFERENCES text_runs(id),
    target_language TEXT NOT NULL,      -- 'es' or 'ar'
    translated_text TEXT,
    engine TEXT,                         -- e.g. "gpt-4o"
    status TEXT DEFAULT 'pending',       -- pending / translated / error / skipped
    error_message TEXT,
    translated_at TEXT,
    UNIQUE(text_run_id, target_language)
);

-- Which page/frame each story is physically placed on, parsed from
-- Spreads/*.xml (a story can flow across multiple frames/pages, so one
-- story_id can have multiple rows here).
CREATE TABLE story_placements (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL REFERENCES stories(id),
    page_name TEXT,                     -- e.g. "236" (Page/@Name in the Spread XML)
    frame_self_id TEXT NOT NULL         -- e.g. "u1170" (TextFrame/@Self)
);

-- Stage 4 - Exception report: one row per detected translation-quality
-- or layout-risk issue.
CREATE TABLE exceptions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    target_language TEXT NOT NULL,      -- 'es' or 'ar'
    category TEXT NOT NULL,             -- translation_error / missing_translation / layout_risk_heuristic
    severity TEXT NOT NULL,             -- MED / HIGH
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open / resolved / ignored
    created_at TEXT NOT NULL
);

-- Which text_runs a given exception concerns - one issue can span
-- multiple fragments (e.g. every occurrence of an inconsistently
-- translated phrase).
CREATE TABLE exception_fragments (
    id INTEGER PRIMARY KEY,
    exception_id INTEGER NOT NULL REFERENCES exceptions(id),
    text_run_id INTEGER NOT NULL REFERENCES text_runs(id)
);

-- Stage 3b - Overflow handling: the real (font-metric + frame-geometry
-- measured, not heuristic) fit result for one translated fragment.
CREATE TABLE overflow_resolutions (
    id INTEGER PRIMARY KEY,
    text_run_id INTEGER NOT NULL REFERENCES text_runs(id),
    target_language TEXT NOT NULL,
    resolution TEXT NOT NULL,        -- fits / fits_wrapped / fits_shrunk / fits_retranslated / unresolved
    original_point_size REAL,
    final_point_size REAL,           -- may be reduced (tier 3)
    shrink_pct REAL,
    retranslated_text TEXT,          -- set only if tier 4 (retranslate for brevity) ran
    lines_needed INTEGER,
    frame_width_pt REAL,
    frame_height_pt REAL,
    text_width_pt REAL,
    font_family TEXT,
    font_style TEXT,
    font_is_substitute INTEGER,      -- 1 if measured with a substitute font, not the real licensed one
    checked_at TEXT,
    UNIQUE(text_run_id, target_language)
);
