-- Synthetic V500 table schema for the Oracle Health / Cerner Millennium EHI
-- single-patient export fixture. Column names and table classifications follow
-- docs/vendor_refs/ORACLE_EHI_SCHEMA.md (§3.2, §4); audit columns (§3.2) are
-- included to exercise the lossless-extensions path. Synthetic data only.
SET FOREIGN_KEY_CHECKS = 0;

-- §3.2 dms_person3.html (subset; PERSON has 89 cols in the real model)
CREATE TABLE `PERSON` (
  `PERSON_ID` DOUBLE NOT NULL,
  `NAME_FULL_FORMATTED` VARCHAR(100),
  `BIRTH_DT_TM` DATETIME,
  `SEX_CD` DOUBLE,
  `DECEASED_DT_TM` DATETIME,
  `LANGUAGE_CD` DOUBLE,
  `UPDT_DT_TM` DATETIME,
  `UPDT_CNT` DOUBLE,
  PRIMARY KEY (`PERSON_ID`)
) ENGINE=InnoDB;

-- PERSON_ALIAS: the brief names this table (MRN) but does not enumerate its
-- columns; spellings here are SYNTHETIC and the adapter routes them losslessly.
CREATE TABLE `PERSON_ALIAS` (
  `PERSON_ALIAS_ID` DOUBLE NOT NULL,
  `PERSON_ID` DOUBLE NOT NULL,
  `ALIAS` VARCHAR(200),
  `PERSON_ALIAS_TYPE_CD` DOUBLE,
  `UPDT_CNT` DOUBLE,
  PRIMARY KEY (`PERSON_ALIAS_ID`)
) ENGINE=InnoDB;

-- §3.2 dms_encounter17.html (subset; 159 cols in the real model)
CREATE TABLE `ENCOUNTER` (
  `ENCNTR_ID` DOUBLE NOT NULL,
  `PERSON_ID` DOUBLE NOT NULL,
  `ENCNTR_TYPE_CD` DOUBLE,
  `REG_DT_TM` DATETIME,
  `DISCH_DT_TM` DATETIME,
  `REASON_FOR_VISIT` VARCHAR(255),
  `UPDT_DT_TM` DATETIME,
  `UPDT_CNT` DOUBLE,
  PRIMARY KEY (`ENCNTR_ID`)
) ENGINE=InnoDB;

-- §3.2 dms_clinical_events10.html (subset; 77 cols in the real model)
CREATE TABLE `CLINICAL_EVENT` (
  `EVENT_ID` DOUBLE NOT NULL,
  `PERSON_ID` DOUBLE NOT NULL,
  `ENCNTR_ID` DOUBLE,
  `EVENT_CD` DOUBLE,
  `EVENT_CLASS_CD` DOUBLE,
  `EVENT_TITLE_TEXT` VARCHAR(255),
  `PARENT_EVENT_ID` DOUBLE,
  `EVENT_RELTN_CD` DOUBLE,
  `RESULT_VAL` VARCHAR(255),
  `RESULT_UNITS_CD` DOUBLE,
  `RESULT_STATUS_CD` DOUBLE,
  `EVENT_END_DT_TM` DATETIME,
  `SERIES_REF_NBR` DOUBLE,
  `VALID_FROM_DT_TM` DATETIME,
  `VALID_UNTIL_DT_TM` DATETIME,
  `UPDT_CNT` DOUBLE,
  PRIMARY KEY (`EVENT_ID`)
) ENGINE=InnoDB;

-- §4.1 dms_clinical_events1.html — locally stored document text
CREATE TABLE `CE_BLOB` (
  `EVENT_ID` DOUBLE NOT NULL,
  `VALID_FROM_DT_TM` DATETIME,
  `BLOB_SEQ_NUM` DOUBLE,
  `BLOB_CONTENTS` LONGBLOB,
  `COMPRESSION_CD` DOUBLE,
  `UPDT_CNT` DOUBLE
) ENGINE=InnoDB;

-- §4.2 dms_clinical_events1.html — remotely stored document handles
CREATE TABLE `CE_BLOB_RESULT` (
  `EVENT_ID` DOUBLE NOT NULL,
  `CONTRIBUTOR_SYSTEM_CD` DOUBLE,
  `SERIES_REF_NBR` DOUBLE,
  `BLOB_HANDLE` VARCHAR(2000),
  `FORMAT_CD` DOUBLE,
  `STORAGE_CD` DOUBLE,
  `UPDT_CNT` DOUBLE
) ENGINE=InnoDB;

-- §3.2 dms_code_sets2.html (subset; CODE_VALUE has 27 cols)
CREATE TABLE `CODE_VALUE` (
  `CODE_VALUE` DOUBLE NOT NULL,
  `CODE_SET` DOUBLE,
  `DISPLAY` VARCHAR(40),
  `DESCRIPTION` VARCHAR(60),
  `DEFINITION` VARCHAR(100),
  `CDF_MEANING` VARCHAR(12),
  PRIMARY KEY (`CODE_VALUE`)
) ENGINE=InnoDB;

SET FOREIGN_KEY_CHECKS = 1;
