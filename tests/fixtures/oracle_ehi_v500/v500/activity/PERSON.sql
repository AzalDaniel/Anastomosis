-- Synthetic PERSON rows (ACTIVITY). §3.2 dms_person3.html. Synthetic ids in
-- the 900000001+ range; Testpatient-style names; example.com is unused here.
-- Patient 900000002 carries a NULL formatted name and a deceased instant.
INSERT INTO `PERSON` (`PERSON_ID`, `NAME_FULL_FORMATTED`, `BIRTH_DT_TM`, `SEX_CD`, `DECEASED_DT_TM`, `LANGUAGE_CD`, `UPDT_DT_TM`, `UPDT_CNT`) VALUES
(900000001,'Testpatient, Alpha Q','1985-03-14 00:00:00',362,NULL,151,'2024-01-02 10:00:00',3),
(900000002,'Sampleson, Beta','1958-11-30 00:00:00',361,'2024-03-01 04:15:00',151,'2024-03-02 08:00:00',5);
