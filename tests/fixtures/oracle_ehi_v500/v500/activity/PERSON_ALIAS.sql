-- Synthetic PERSON_ALIAS rows (ACTIVITY). The brief names this table for the
-- MRN but does NOT enumerate its columns (§3.2 cites PERSON/ENCOUNTER columns
-- only); these column spellings are SYNTHETIC and the adapter preserves every
-- value losslessly as an OTHER identifier rather than asserting "this is MRN".
INSERT INTO `PERSON_ALIAS` (`PERSON_ALIAS_ID`, `PERSON_ID`, `ALIAS`, `PERSON_ALIAS_TYPE_CD`, `UPDT_CNT`) VALUES
(900200001,900000001,'MRN900000001',10,1),
(900200002,900000001,'FIN-ALPHA-001',20,1),
(900200003,900000002,'MRN900000002',10,1);
