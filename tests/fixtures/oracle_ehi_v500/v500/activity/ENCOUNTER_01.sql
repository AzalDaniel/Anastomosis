-- Synthetic ENCOUNTER rows, file 1 of 2 (ACTIVITY). §3.2 dms_encounter17.html.
-- §5.1 allows 1..N files per table; this fixture splits ENCOUNTER to exercise
-- multi-file reads. REASON_FOR_VISIT is the free-text visit reason (§3.2).
INSERT INTO `ENCOUNTER` (`ENCNTR_ID`, `PERSON_ID`, `ENCNTR_TYPE_CD`, `REG_DT_TM`, `DISCH_DT_TM`, `REASON_FOR_VISIT`, `UPDT_DT_TM`, `UPDT_CNT`) VALUES
(900100001,900000001,5001,'2024-02-05 14:30:00',NULL,'Annual wellness visit','2024-02-05 16:00:00',2),
(900100002,900000001,5002,'2024-04-12 09:15:00','2024-04-12 11:45:00','Follow-up hypertension','2024-04-12 12:00:00',1);
