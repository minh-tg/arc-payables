-- The unsent draft note on an exception case. The note is advisory text only: it is sent to no
-- one, and it changes no check, approval or payment. It is cleared whenever the case's items are
-- rewritten, because a note about the previous failing checks would no longer describe the case.
-- A case opened before this column existed keeps NULL, which renders no draft.
ALTER TABLE exception_cases ADD COLUMN draft_json TEXT;
