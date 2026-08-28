-- Roll back only the v168 marker.
--
-- The gui_events table is canonically owned by migration 163_gui_events.sql.
-- Dropping it here would corrupt fresh databases where v163 legitimately created
-- the table before this safety backfill ran.

DELETE FROM schema_versions WHERE version = 168;
