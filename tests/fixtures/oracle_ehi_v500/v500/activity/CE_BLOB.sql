-- Synthetic CE_BLOB rows (ACTIVITY). §4.1 dms_clinical_events1.html: locally
-- stored document text keyed to a clinical event. BLOB_CONTENTS is "Text of
-- the blob"; COMPRESSION_CD = NULL means uncompressed here. The note body uses
-- a doubled '' apostrophe escape and an HTML fragment to exercise the lexer
-- and html_to_text. BLOB_SEQ_NUM 1 + 2 prove multi-blob concatenation.
--
-- Event 900300001 also has a SECOND, COMPRESSED blob (COMPRESSION_CD set):
-- the brief lists the COMPRESSION_CD code set + algorithm as could-not-
-- determine (§8), so the adapter refuses to decode it (loud NotImplementedError
-- caught and preserved in extensions) rather than guessing. The superseded
-- event 900300010 carries the older revision of the note.
INSERT INTO `CE_BLOB` (`EVENT_ID`, `VALID_FROM_DT_TM`, `BLOB_SEQ_NUM`, `BLOB_CONTENTS`, `COMPRESSION_CD`, `UPDT_CNT`) VALUES
(900300001,'2024-02-05 15:40:00',1,'<p>Patient reports feeling well. No new complaints.</p>',NULL,1),
(900300001,'2024-02-05 15:40:00',2,'<p>Plan: continue current regimen. Patient''s questions addressed.</p>',NULL,1),
(900300001,'2024-02-05 15:40:00',3,'COMPRESSED-BINARY-PLACEHOLDER',2099,1),
(900300010,'2024-02-04 09:00:00',1,'<p>Earlier draft of the progress note.</p>',NULL,1);
