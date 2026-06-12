-- Synthetic ENCOUNTER rows, file 2 of 2 (ACTIVITY). This INSERT names its own
-- column list in a DIFFERENT order than the DDL, to prove the loader honors
-- the statement's column ordering over the schema default.
INSERT INTO `ENCOUNTER` (`PERSON_ID`, `ENCNTR_ID`, `REASON_FOR_VISIT`, `ENCNTR_TYPE_CD`, `REG_DT_TM`, `DISCH_DT_TM`, `UPDT_DT_TM`, `UPDT_CNT`) VALUES
(900000002,900100003,'Chest pain evaluation',5003,'2024-03-20 22:05:00','2024-03-21 06:30:00','2024-03-21 07:00:00',4);
