-- Synthetic CE_BLOB_RESULT rows (ACTIVITY). §4.2 dms_clinical_events1.html:
-- one row per physical remote document; BLOB_HANDLE is the "handle to remote
-- blob" and STORAGE_CD resolves (via code set 25, CDF_MEANING) to where the
-- handle points -- DICOM_SIUID => the handle is a DICOM study UID (§4.2). The
-- adapter records the handle as a DocumentArtifact REFERENCE and never fetches
-- it. The handle below is a synthetic DICOM-study-UID-shaped string.
INSERT INTO `CE_BLOB_RESULT` (`EVENT_ID`, `CONTRIBUTOR_SYSTEM_CD`, `SERIES_REF_NBR`, `BLOB_HANDLE`, `FORMAT_CD`, `STORAGE_CD`, `UPDT_CNT`) VALUES
(900300006,9100,5,'1.2.840.10008.5.1.4.900000.1.2.3',9200,2501,1);
