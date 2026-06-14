-- Synthetic CODE_VALUE rows (REFERENCE). §3.2 dms_code_sets2.html: the
-- dictionary every *_CD numeric key resolves against. DISPLAY is the human
-- label; CODE_SET + CDF_MEANING drive storage-location logic (§4.2). Code set
-- 25 holds the blob STORAGE_CD meanings; 2501 => DICOM_SIUID. Synthetic codes.
INSERT INTO `CODE_VALUE` (`CODE_VALUE`, `CODE_SET`, `DISPLAY`, `DESCRIPTION`, `DEFINITION`, `CDF_MEANING`) VALUES
(361,57,'Male','Male','Administrative sex male','MALE'),
(362,57,'Female','Female','Administrative sex female','FEMALE'),
(151,36,'English','English','Preferred language English','ENGLISH'),
(5001,69,'Wellness Visit','Wellness Visit','Annual wellness encounter','WELLNESS'),
(5002,69,'Office Visit','Office Visit','Ambulatory office encounter','OFFICE'),
(5003,69,'Emergency','Emergency','Emergency department encounter','EMERGENCY'),
(2501,25,'Image Server','Image Server','Remote DICOM image storage','DICOM_SIUID'),
(2502,25,'Document Imaging','Document Imaging','OnBase / Document Imaging store','OTG');
