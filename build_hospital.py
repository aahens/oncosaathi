"""Write hospital.json from the Tata Memorial Hospital Patient Booklet 2025 (public PDF on tmc.gov.in).

Run after editing the table below:   python build_hospital.py   (then python make_floor_plans.py)

What is real and what is drawn:
- From the booklet: the 5 buildings, which department is in which building and on which floor, separate locations for
  general and private patients, counter / room numbers, timings and what to bring where the booklet gives them
  (section 2 "Departments/Services Location Directory", 2.1 OPD counters, 3.1 registration, 6.2 imaging, 6.3 reports,
  14.1 cash counter, 23 emergencies, 26 Kevat navigators). Not verified or endorsed by the hospital.
- Drawn by us: where the buildings stand on the campus and where a department sits inside its floor. The booklet has no
  map, so the maps say "positions are illustrative" and the walking text only says which building, which floor, and
  "follow the signs".
- Departments people ask about that the booklet does not list (endoscopy, ultrasound...) are kept as "unlisted":
  OncoSaathi says so and sends them to the Helpdesk or a Kevat navigator instead of guessing.

Unit ids are kept from the earlier sample layout where the department is the same (C-1-MRI, B-G-SAMPLE...), so the
rest of the code keeps working; the letters in them no longer mean a wing or floor.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = "Tata Memorial Hospital Patient Booklet 2025, https://tmc.gov.in/event/upload/12112025_5/TMH_Brochure_%20patient%20booklet.pdf"

BUILDINGS = [  # id, English, Hindi, floors that exist (from the booklet)
    ("MB", "Main Building (MB)", "मेन बिल्डिंग (MB)", ["B", "G", "1", "2", "3"]),
    ("HBB", "Homi Bhabha Building (HBB)", "होमी भाभा बिल्डिंग (HBB)", ["B", "G", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]),
    ("GJB", "Golden Jubilee Building (GJB)", "गोल्डन जुबली बिल्डिंग (GJB)", ["B", "G", "1", "2", "3", "4", "5", "6", "7"]),
    ("AB", "Annexe Building (AB)", "एनेक्स बिल्डिंग (AB)", ["B", "G", "1", "2", "3", "4", "5", "6", "7", "8"]),
    ("SB", "Service Building (SB)", "सर्विस बिल्डिंग (SB)", ["G", "1", "2", "3", "4", "5", "6"]),
]
FLOORS = [("B", "Basement", "बेसमेंट"), ("G", "Ground floor", "ग्राउंड फ़्लोर"), ("1", "First floor", "पहली मंज़िल"),
          ("2", "Second floor", "दूसरी मंज़िल"), ("3", "Third floor", "तीसरी मंज़िल"), ("4", "Fourth floor", "चौथी मंज़िल"),
          ("5", "Fifth floor", "पाँचवीं मंज़िल"), ("6", "Sixth floor", "छठी मंज़िल"), ("7", "Seventh floor", "सातवीं मंज़िल"),
          ("8", "Eighth floor", "आठवीं मंज़िल"), ("9", "Ninth floor", "नौवीं मंज़िल"), ("10", "Tenth floor", "दसवीं मंज़िल")]
CATEGORIES = {"opd": ("OPD / consultation", "ओपीडी / डॉक्टर से परामर्श"), "diagnostic": ("Tests & scans", "जाँच और स्कैन"),
              "treatment": ("Treatment", "इलाज"), "emergency": ("Emergency", "इमरजेंसी"),
              "support": ("Patient services", "मरीज़ सेवाएँ"), "ward": ("Wards & staff-only areas", "वार्ड / सिर्फ़ स्टाफ़")}


def L(block, floor, rooms="", rooms_hi=None):
    """One location: building, floor, counter / room text (English, Hindi)."""
    return {"block": block, "floor": floor, "rooms": rooms, "rooms_hi": rooms_hi if rooms_hi is not None else rooms}


def U(id, cat, name, name_hi, at, also=(), private=None, src="", hours=None, bring=None, note=None, public=True):
    u = {"id": id, "cat": cat, "name": name, "name_hi": name_hi, **at}
    if also:
        u["also"] = list(also)
    if private:
        u["private"] = private
    for key, val in (("hours", hours), ("bring", bring), ("note", note)):
        if val:
            u[key], u[key + "_hi"] = val
    if not public:
        u["public"] = False
    u["source"] = src
    return u


def ASK(id, name, name_hi, ask, why, why_hi):
    """A department the booklet does not list: say so and send them to someone who knows (no map of our own)."""
    return {"id": id, "cat": "support", "name": name, "name_hi": name_hi, "unlisted": True, "ask": ask,
            "note": why, "note_hi": why_hi, "source": "Not listed in the booklet"}


TOKEN_NOTE = ("First take a token at the Rotunda, Homi Bhabha Building first floor (counters 129 to 131, 8:30 am to 5:30 pm). "
              "Bring the requisition from your doctor.",
              "पहले होमी भाभा बिल्डिंग की पहली मंज़िल पर रोटुंडा से टोकन लीजिए (काउंटर 129 से 131, सुबह 8:30 से शाम 5:30)। "
              "डॉक्टर की लिखी रिक्विज़िशन साथ लाइए।")
DMG_NOTE = ("Your OPD counter depends on your cancer type (your DMG, written on your case file). Tell me your cancer type, "
            "for example breast or head and neck, and I'll give you the exact counter.",
            "आपका ओपीडी काउंटर आपके कैंसर के प्रकार (DMG) पर निर्भर है, जो आपकी केस फ़ाइल पर लिखा होता है। अपना कैंसर का प्रकार "
            "बताइए, जैसे ब्रेस्ट या हेड एंड नेक, तो मैं सही काउंटर बता दूँगी।")

UNITS = [
    # ---- arriving, registration, money ----
    U("A-G-HELP", "support", "Helpdesk", "हेल्पडेस्क", L("HBB", "1", "Rotunda", "रोटुंडा"), src="§2 #24",
      note=("Run by NGOs. General information about the hospital.", "एनजीओ चलाते हैं। अस्पताल के बारे में आम जानकारी मिलती है।")),
    U("HELPLINE", "support", "Helpline (wheelchair, stretcher, lunch)", "हेल्पलाइन (व्हीलचेयर, स्ट्रेचर, खाना)",
      L("AB", "G", "AB premises", "AB परिसर"), src="§2 #23",
      note=("Ask here for a wheelchair or stretcher, lunch and snacks.", "व्हीलचेयर, स्ट्रेचर, दोपहर का खाना और नाश्ते के लिए यहाँ पूछिए।")),
    U("KEVAT", "support", "Patient navigators (Kevat)", "पेशेंट नेविगेटर (केवट)", L("GJB", "G", "Counter 50", "काउंटर 50"), src="§26",
      note=("Each OPD also has its own Kevat. Look for a green or white uniform.",
            "हर ओपीडी का अपना केवट भी होता है। हरी या सफ़ेद यूनिफ़ॉर्म देखिए।")),
    U("A-G-REG", "support", "New patient registration", "नए मरीज़ का रजिस्ट्रेशन", L("GJB", "G", "OPD registration counters", "ओपीडी रजिस्ट्रेशन काउंटर"),
      private=L("HBB", "1", "Registration counters", "रजिस्ट्रेशन काउंटर"), src="§3.1, §3.6",
      hours=("New patients: 8 am to 11 am", "नए मरीज़: सुबह 8 से 11 बजे तक"),
      bring=("Aadhaar or another government photo ID of the patient and a nominee, the referral letter if you have one, "
             "and all old reports (scans, blood tests, pathology slides or blocks). Come with a responsible adult.",
             "मरीज़ और नॉमिनी का आधार या कोई सरकारी फ़ोटो आईडी, रेफ़रल लेटर हो तो वह, और सारी पुरानी रिपोर्ट (स्कैन, खून की जाँच, "
             "पैथोलॉजी स्लाइड या ब्लॉक)। किसी ज़िम्मेदार बड़े व्यक्ति को साथ लाइए।"),
      note=("You can register online first to save time: tmc.gov.in, New Registration. After registration you get a smart card "
            "and a case file.", "समय बचाने के लिए पहले ऑनलाइन रजिस्टर कर सकते हैं: tmc.gov.in, New Registration। रजिस्ट्रेशन के बाद "
            "स्मार्ट कार्ड और केस फ़ाइल मिलती है।")),
    U("SMARTCARD", "support", "New smart card for old registered patients", "पुराने मरीज़ों के लिए नया स्मार्ट कार्ड",
      L("GJB", "G"), private=L("HBB", "1"), src="§2 #37"),
    U("A-G-BILL", "support", "Cash deposit counter (smart card top-up)", "कैश डिपॉज़िट काउंटर (स्मार्ट कार्ड टॉप-अप)",
      L("GJB", "G"), private=L("HBB", "1"), src="§2 #9, §14.1",
      note=("Tests and scans are paid from your smart card; add money to it here.",
            "जाँच और स्कैन का पैसा स्मार्ट कार्ड से कटता है; उसमें पैसे यहाँ जमा कीजिए।")),
    U("ACCOUNTS", "support", "Accounts and billing (smart card refund)", "अकाउंट्स और बिलिंग (स्मार्ट कार्ड रिफ़ंड)",
      L("SB", "1"), src="§2 #1"),
    U("A-G-SW", "support", "Medical Social Worker (help with costs and schemes)", "मेडिकल सोशल वर्कर (ख़र्च और सरकारी योजनाओं में मदद)",
      L("GJB", "G"), src="§2 #32, §17, §18",
      note=("Help with treatment costs, government schemes and a place to stay.", "इलाज के ख़र्च, सरकारी योजनाओं और रहने की जगह में मदद।")),
    U("INSURANCE", "support", "Insurance, mediclaim and reimbursement (CRO)", "इंश्योरेंस, मेडिक्लेम और रिइम्बर्समेंट (CRO)",
      L("HBB", "1", "CRO"), src="§2 #26"),
    U("PRO", "support", "Public Relation Officer (feedback and complaints)", "पब्लिक रिलेशन ऑफ़िसर (सुझाव और शिकायत)",
      L("HBB", "1"), src="§2 #47"),
    U("D-4-ADMIN", "support", "Medical Superintendent", "मेडिकल सुपरिंटेंडेंट", L("HBB", "1"), src="§2 #31"),
    U("ADMIN-OFF", "support", "Administration office", "प्रशासन कार्यालय", L("SB", "4"), src="§2 #3"),
    U("ADMIT", "support", "Admissions desk", "एडमिशन डेस्क", L("HBB", "1"), src="§7.1",
      hours=("9:30 am to 5:30 pm", "सुबह 9:30 से शाम 5:30 तक"),
      bring=("the written admission order from your doctor", "डॉक्टर का लिखा एडमिशन ऑर्डर")),
    U("MEDREC", "support", "Medical records", "मेडिकल रिकॉर्ड", L("AB", "6"), src="§2 #30"),
    U("TELECONSULT", "support", "Teleconsultation enquiry", "टेलीकंसल्टेशन पूछताछ", L("GJB", "G"), src="§2 #59"),
    U("A-G-CANT", "support", "Cafeteria", "कैंटीन", L("GJB", "G"), also=[L("HBB", "1")], src="§2 #8"),
    U("ATM", "support", "ATM", "एटीएम", L("GJB", "G"), also=[L("HBB", "1")], src="§2 #4"),
    U("BANK", "support", "Central Bank branch", "सेंट्रल बैंक शाखा", L("AB", "G"), src="§2 #5"),
    U("STORAGE", "support", "Storage facility (luggage)", "सामान रखने की जगह", L("GJB", "G", "Near the GJB back gate", "GJB के पिछले गेट के पास"),
      src="§2 #57"),
    U("CORE-G-PHARM", "support", "Dispensary (pharmacy)", "डिस्पेंसरी (दवाई की दुकान)", L("GJB", "G", "Counters 54 to 55", "काउंटर 54 से 55"),
      private=L("HBB", "G"), src="§2 #14, §12.6",
      bring=("your smart card", "अपना स्मार्ट कार्ड"),
      note=("An online prescription is valid for 3 days.", "ऑनलाइन पर्ची 3 दिन तक मान्य रहती है।")),
    # ---- tests and scans ----
    U("B-G-SAMPLE", "diagnostic", "Blood and sample collection", "खून और सैंपल कलेक्शन", L("GJB", "1"), private=L("HBB", "1"),
      src="§2 #6, #54"),
    U("B-G-REPORT", "diagnostic", "Report enquiry", "रिपोर्ट पूछताछ", L("GJB", "G"), private=L("HBB", "1"), src="§6.3, §27",
      note=("The hospital does not give paper reports. See them online in Web EMR; common lab reports also come on your "
            "registered WhatsApp number. Ask here if a report is late.",
            "अस्पताल काग़ज़ की रिपोर्ट नहीं देता। रिपोर्ट Web EMR पर ऑनलाइन देखिए; आम लैब रिपोर्ट आपके रजिस्टर्ड WhatsApp नंबर पर भी आती हैं। "
            "रिपोर्ट देर से आए तो यहाँ पूछिए।")),
    U("C-1-RADREC", "diagnostic", "Scan token counter (CT, MRI, PET, SPECT)", "स्कैन टोकन काउंटर (CT, MRI, PET, SPECT)",
      L("HBB", "1", "Rotunda, counters 129 to 131", "रोटुंडा, काउंटर 129 से 131"), src="§6.2",
      hours=("8:30 am to 5:30 pm", "सुबह 8:30 से शाम 5:30 तक"),
      bring=("the requisition from your doctor and your smart card", "डॉक्टर की रिक्विज़िशन और अपना स्मार्ट कार्ड"),
      note=("The scan charge is taken from your smart card here.", "स्कैन का पैसा यहीं स्मार्ट कार्ड से कटता है।")),
    U("C-1-CT", "diagnostic", "CT scan", "सीटी स्कैन", L("MB", "B"), also=[L("GJB", "B")], src="§2 #11, §6.2", note=TOKEN_NOTE),
    U("C-1-MRI", "diagnostic", "MRI scan", "एमआरआई स्कैन", L("GJB", "B"), private=L("MB", "B"), src="§2 #36, §6.2", note=TOKEN_NOTE),
    U("C-G-XRAY", "diagnostic", "General radiology (X-ray)", "जनरल रेडियोलॉजी (एक्स-रे)", L("GJB", "B"), private=L("MB", "G"),
      src="§2 #20"),
    U("C-1-USG", "diagnostic", "Mammography", "मैमोग्राफ़ी", L("MB", "G"), private=L("HBB", "1"), src="§2 #29"),
    U("IR-OPD", "diagnostic", "Interventional radiology OPD", "इंटरवेंशनल रेडियोलॉजी ओपीडी", L("GJB", "B"), private=L("HBB", "3"),
      src="§2 #27"),
    U("B-1-PATH", "diagnostic", "Pathology (biopsy slides and blocks)", "पैथोलॉजी (बायोप्सी स्लाइड और ब्लॉक)", L("AB", "8"), src="§2 #42"),
    U("CYTO", "diagnostic", "Cytopathology", "साइटोपैथोलॉजी", L("AB", "5"), src="§2 #12"),
    U("BIOCHEM", "diagnostic", "Clinical biochemistry lab", "क्लिनिकल बायोकेमिस्ट्री लैब", L("AB", "5"), src="§2 #10"),
    U("MICRO", "diagnostic", "Microbiology", "माइक्रोबायोलॉजी", L("AB", "6"), src="§2 #33"),
    U("MOLPATH", "diagnostic", "Molecular pathology and haematopathology labs", "मॉलिक्यूलर पैथोलॉजी और हीमेटोपैथोलॉजी लैब",
      L("AB", "7"), src="§2 #22, #35"),
    U("B-3-PAC", "diagnostic", "Pre-anaesthesia check-up (PAC)", "ऑपरेशन से पहले की जाँच (PAC)", L("GJB", "B"), private=L("HBB", "G"),
      src="§2 #44"),
    U("B-3-CARDIO", "diagnostic", "ECG", "ईसीजी", L("GJB", "7"), private=L("HBB", "G"), src="§2 #17"),
    U("ECHO", "diagnostic", "2D Echo", "2D इको", L("GJB", "B"),
      private=L("MB", "1", "Also on the HBB ground floor", "HBB ग्राउंड फ़्लोर पर भी"), src="§2 #16"),
    U("PFT", "diagnostic", "Pulmonary function test (lung test)", "पल्मोनरी फ़ंक्शन टेस्ट (फेफड़ों की जाँच)", L("MB", "1"), src="§2 #48"),
    # ---- OPD (by cancer type, from the booklet's counter tables) ----
    U("A-3-BR", "opd", "Breast OPD", "ब्रेस्ट ओपीडी", L("GJB", "5", "Rooms 508 to 510", "कमरा 508 से 510"),
      private=L("HBB", "1", "Counters 109 and 112", "काउंटर 109 और 112"), src="§2.1"),
    U("A-3-HN", "opd", "Head and neck OPD", "हेड एंड नेक ओपीडी", L("GJB", "1", "Rooms 108A to 111", "कमरा 108A से 111"),
      private=L("HBB", "2", "Counters 207 and 210", "काउंटर 207 और 210"), src="§2.1"),
    U("GASTRO-OPD", "opd", "Gastro (GI) OPD", "गैस्ट्रो (पेट) ओपीडी", L("GJB", "1", "Rooms 120 to 124", "कमरा 120 से 124"),
      private=L("HBB", "3", "Counters 312 and 314", "काउंटर 312 और 314"), src="§2.1"),
    U("A-3-GYN", "opd", "Gynaecologic oncology OPD", "स्त्री रोग कैंसर (गायनेक) ओपीडी", L("GJB", "5", "Rooms 502 to 504", "कमरा 502 से 504"),
      private=L("HBB", "G", "Counter 61", "काउंटर 61"), src="§2.1"),
    U("URO-OPD", "opd", "Uro-oncology OPD", "यूरो-ऑन्कोलॉजी ओपीडी", L("GJB", "5", "Rooms 502 to 504", "कमरा 502 से 504"),
      private=L("HBB", "2", "Counter 214", "काउंटर 214"), src="§2.1"),
    U("THOR-OPD", "opd", "Thoracic (chest and lung) OPD", "थोरेसिक (छाती और फेफड़े) ओपीडी", L("GJB", "G", "Rooms 67 to 69", "कमरा 67 से 69"),
      private=L("HBB", "3", "Counters 307 and 310", "काउंटर 307 और 310"), src="§2.1"),
    U("BONE-OPD", "opd", "Bone and soft tissue OPD", "हड्डी और सॉफ़्ट टिश्यू ओपीडी", L("GJB", "G", "Rooms 67 to 69", "कमरा 67 से 69"),
      also=[L("MB", "G", "Counter 93", "काउंटर 93")], src="§2.1"),
    U("NEURO-OPD", "opd", "Neuro-oncology OPD", "न्यूरो-ऑन्कोलॉजी ओपीडी", L("HBB", "G", "Counter 58", "काउंटर 58"), src="§2.1"),
    U("HEME-OPD", "opd", "Adult haematology OPD (blood cancers)", "एडल्ट हीमेटोलॉजी ओपीडी (ब्लड कैंसर)",
      L("GJB", "1", "Rooms 140 to 142", "कमरा 140 से 142"), also=[L("MB", "G", "Counter 81", "काउंटर 81")], src="§2.1"),
    U("A-2-PEDS", "opd", "Paediatric oncology OPD (children)", "बच्चों का कैंसर ओपीडी",
      L("MB", "G", "Counters 80 (solid tumours) and 88 (blood cancers)", "काउंटर 80 (ठोस ट्यूमर) और 88 (ब्लड कैंसर)"), src="§2.1"),
    U("DENTAL", "opd", "Dental department", "डेंटल विभाग", L("GJB", "1", "Room 130", "कमरा 130"),
      private=L("HBB", "2", "Counter 212", "काउंटर 212"), src="§2 #15, §2.1"),
    # ---- treatment and care ----
    U("A-2-DC", "treatment", "Chemotherapy daycare", "कीमोथेरेपी डेकेयर", L("AB", "6"), private=L("HBB", "5"), src="§2 #13",
      note=("Your token is issued one hour before your appointment, so please don't come earlier than that. "
            "Show your appointment slip at the entrance gate.",
            "टोकन अपॉइंटमेंट से एक घंटा पहले मिलता है, इसलिए उससे पहले मत आइए। मेन गेट पर अपॉइंटमेंट की पर्ची दिखाइए।")),
    U("D-G-RT", "treatment", "Radiotherapy department", "रेडियोथेरेपी विभाग", L("MB", "B"), also=[L("HBB", "B"), L("AB", "B")],
      src="§2 #52, §10",
      note=("Your doctors will give you the treatment plan and the schedule for your radiation.",
            "आपके डॉक्टर रेडिएशन का प्लान और समय बता देंगे।")),
    U("RMC", "treatment", "Radiation Medicine Centre", "रेडिएशन मेडिसिन सेंटर", L("AB", "G", "Ground to fourth floor", "ग्राउंड से चौथी मंज़िल"),
      src="§2 #51"),
    U("A-4-PALL", "treatment", "Palliative care", "पैलिएटिव केयर", L("MB", "G"), src="§2 #41"),
    U("PAIN", "treatment", "Pain clinic", "पेन क्लिनिक", L("GJB", "B"), private=L("HBB", "G"), src="§2 #40"),
    U("A-4-PSY", "support", "Psychiatry and counselling", "मनोरोग और काउंसलिंग", L("GJB", "7"), src="§2 #46"),
    U("A-4-NUT", "support", "Nutrition clinic", "पोषण (डाइट) क्लिनिक", L("GJB", "1"), private=L("HBB", "3"), src="§2 #38"),
    U("PHYSIO", "treatment", "Physiotherapy", "फ़िज़ियोथेरेपी", L("MB", "G"), src="§2 #43"),
    U("OCC-THER", "treatment", "Occupational therapy", "ऑक्यूपेशनल थेरेपी", L("MB", "G"), src="§2 #39"),
    U("SPEECH", "treatment", "Speech therapy", "स्पीच थेरेपी", L("GJB", "B"), src="§2 #56"),
    U("STOMA", "treatment", "Stoma clinic", "स्टोमा क्लिनिक", L("HBB", "3"), src="§2 #58"),
    U("PREVENT", "opd", "Preventive oncology (cancer screening)", "प्रिवेंटिव ऑन्कोलॉजी (कैंसर स्क्रीनिंग)", L("SB", "3"), src="§2 #45, §5.1"),
    U("GENMED", "opd", "General medicine (physician)", "जनरल मेडिसिन (फ़िज़िशियन)", L("GJB", "7"), private=L("MB", "1"), src="§2 #21"),
    U("PULM", "opd", "Pulmonary medical unit", "पल्मोनरी मेडिकल यूनिट", L("GJB", "4"), src="§2 #49"),
    U("BLOODBANK", "support", "Blood centre (blood donation)", "ब्लड सेंटर (रक्तदान)", L("SB", "5"), also=[L("MB", "3")],
      src="§2 #7, #61, §13"),
    U("D-G-ER", "emergency", "Emergency (Casualty)", "इमरजेंसी (कैज़ुअल्टी)", L("MB", "G"), src="§2 #18, §23",
      hours=("Open 24 hours, every day", "24 घंटे, हर दिन"),
      note=("For guidance call the hospital board number 022 2417 7000.", "मदद के लिए अस्पताल के बोर्ड नंबर 022 2417 7000 पर फ़ोन कीजिए।")),
    U("D-2-ICU", "ward", "ICU", "आईसीयू", L("MB", "1"), also=[L("MB", "2")], src="§2 #25", public=False),
    U("D-1-OT", "ward", "Main operation theatre", "मेन ऑपरेशन थिएटर", L("MB", "2"), private=L("HBB", "4"), src="§2 #28", public=False),
    U("MINOR-OT", "treatment", "Minor operation theatre", "माइनर ऑपरेशन थिएटर", L("GJB", "3"), also=[L("HBB", "4")], src="§2 #34"),
]

# OPDs asked about without the cancer type: no single place, so no map; the DMG note asks for the type.
for uid, name, name_hi, syn in (("A-1-MED", "Medical oncology OPD", "मेडिकल ऑन्कोलॉजी ओपीडी", ""),
                                ("A-1-SURG", "Surgical oncology OPD", "सर्जिकल ऑन्कोलॉजी ओपीडी", ""),
                                ("A-1-RTC", "Radiation oncology OPD", "रेडिएशन ऑन्कोलॉजी ओपीडी", "")):
    UNITS.append({"id": uid, "cat": "opd", "name": name, "name_hi": name_hi, "dmg": True,
                  "where": "General patients: Golden Jubilee Building (GJB), ground, first or fifth floor. Private patients: "
                           "Homi Bhabha Building (HBB), ground to third floor.",
                  "where_hi": "जनरल मरीज़: गोल्डन जुबली बिल्डिंग (GJB), ग्राउंड, पहली या पाँचवीं मंज़िल। प्राइवेट मरीज़: होमी भाभा बिल्डिंग "
                              "(HBB), ग्राउंड से तीसरी मंज़िल।",
                  "note": DMG_NOTE[0], "note_hi": DMG_NOTE[1], "source": "§2.1"})

UNITS += [
    ASK("C-2-PET", "PET or SPECT scan", "PET या SPECT स्कैन", "C-1-RADREC",
        "The booklet doesn't say where the PET machine is. First take your token at the scan token counter; they will tell you where to go.",
        "बुकलेट में PET मशीन की जगह नहीं लिखी है। पहले स्कैन टोकन काउंटर से टोकन लीजिए; वहाँ आगे की जगह बता देंगे।"),
    ASK("B-2-ENDO", "Endoscopy", "एंडोस्कोपी", "A-G-HELP",
        "The booklet doesn't list where endoscopy is. Please ask at the Helpdesk or a Kevat navigator.",
        "बुकलेट में एंडोस्कोपी की जगह नहीं लिखी है। हेल्पडेस्क या किसी केवट से पूछिए।"),
    ASK("USG", "Ultrasound (sonography)", "अल्ट्रासाउंड (सोनोग्राफ़ी)", "A-G-HELP",
        "The booklet doesn't list where ultrasound is. Please ask at the Helpdesk or a Kevat navigator.",
        "बुकलेट में अल्ट्रासाउंड की जगह नहीं लिखी है। हेल्पडेस्क या किसी केवट से पूछिए।"),
    ASK("B-4-GEN", "Genetic counselling", "जेनेटिक काउंसलिंग", "A-G-HELP",
        "The booklet doesn't list genetic counselling. Please ask your doctor or the Helpdesk.",
        "बुकलेट में जेनेटिक काउंसलिंग नहीं लिखी है। अपने डॉक्टर या हेल्पडेस्क से पूछिए।"),
    ASK("A-4-TOB", "Tobacco cessation", "तंबाकू छुड़ाने की मदद", "A-G-HELP",
        "The booklet doesn't list a tobacco clinic. Please ask at the Helpdesk.",
        "बुकलेट में तंबाकू छुड़ाने का क्लिनिक नहीं लिखा है। हेल्पडेस्क पर पूछिए।"),
    ASK("D-1-OTWAIT", "Family waiting area during surgery", "ऑपरेशन के समय परिवार के बैठने की जगह", "A-G-HELP",
        "The booklet doesn't say where families wait during an operation. Please ask the ward staff or the Helpdesk.",
        "बुकलेट में ऑपरेशन के समय परिवार के बैठने की जगह नहीं लिखी है। वार्ड स्टाफ़ या हेल्पडेस्क से पूछिए।"),
    ASK("C-3-SURGW", "Wards", "वार्ड", "ADMIT",
        "Your ward and bed are written on your admission papers. The Admissions desk can tell you.",
        "आपका वार्ड और बेड आपके एडमिशन के काग़ज़ों पर लिखा होता है। एडमिशन डेस्क बता देगा।"),
    ASK("A-G-PMJAY", "Ayushman Bharat (PM-JAY)", "आयुष्मान भारत (PM-JAY)", "A-G-SW",
        "The booklet doesn't list a PM-JAY desk. The Medical Social Worker helps with government schemes.",
        "बुकलेट में PM-JAY काउंटर नहीं लिखा है। सरकारी योजनाओं में मेडिकल सोशल वर्कर मदद करते हैं।"),
]

CAMPUS = [
    {"id": "GATE1", "cat": "support", "name": "Main gate (Dr Ernest Borges Road)", "name_hi": "मेन गेट (डॉ. अर्नेस्ट बोर्जेस रोड)",
     "note": "Show your appointment slip or smart card at the entrance gate.", "note_hi": "मेन गेट पर अपनी अपॉइंटमेंट की पर्ची या स्मार्ट कार्ड दिखाइए।",
     "source": "§3.6, §8"},
    {"id": "GATE2", "cat": "support", "name": "GJB back gate", "name_hi": "GJB का पिछला गेट",
     "note": "The back gate of the Golden Jubilee Building.", "note_hi": "गोल्डन जुबली बिल्डिंग का पिछला गेट।", "source": "§2 #57"},
]


def ordinal_floor(floor, hi):
    return next((h if hi else e.lower() for f, e, h in FLOORS if f == floor), floor)


def building_name(block, hi):
    return next((h if hi else e for b, e, h, _ in BUILDINGS if b == block), block)


def path_text(u, at, hi):
    """Walking words from the main gate: building, floor, then the signs. Inside a floor we only know the floor."""
    b, f = building_name(at["block"], hi), ordinal_floor(at["floor"], hi)
    name = u["name_hi"] if hi else u["name"]
    rooms = at.get("rooms_hi" if hi else "rooms") or ""
    if hi:
        lift = {"G": "यह ग्राउंड फ़्लोर पर है।", "B": "लिफ़्ट से नीचे बेसमेंट में जाइए।"}.get(at["floor"], f"लिफ़्ट से {f} पर जाइए।")
        return f"मेन गेट से {b} तक चलिए। {lift} फिर {name} के बोर्ड देखते हुए जाइए{(', ' + rooms) if rooms else ''}।"
    lift = {"G": "It is on the ground floor.", "B": "Take the lift down to the basement."}.get(at["floor"], f"Take the lift to the {f}.")
    return f"From the main gate, walk to the {b}. {lift} Then follow the signs to {name}{(', ' + rooms) if rooms else ''}."


def main():
    units = []
    for u in UNITS:
        if "block" in u:
            u["path"], u["path_hi"] = path_text(u, u, False), path_text(u, u, True)
            if "private" in u:
                p = u["private"]
                p["path"], p["path_hi"] = path_text(u, p, False), path_text(u, p, True)
            for a in u.get("also", []):
                a["path"], a["path_hi"] = path_text(u, a, False), path_text(u, a, True)
        units.append(u)
    ids = [u["id"] for u in units + CAMPUS]
    assert len(ids) == len(set(ids)), "duplicate ids"
    used = {(u["block"], u["floor"]) for u in units if "block" in u} | \
           {(x["block"], x["floor"]) for u in units for x in u.get("also", []) + ([u["private"]] if "private" in u else [])}
    for b, f in used:
        assert f in next(fl for bid, _, _, fl in BUILDINGS if bid == b), (b, f)
    out = {
        "name": "Tata Memorial Hospital, Parel", "name_hi": "टाटा मेमोरियल हॉस्पिटल, परेल", "city": "Mumbai",
        "disclaimer": "Buildings, floors and counters are from the public TMH Patient Booklet 2025, not verified by the hospital. "
                      "Building positions and room positions inside a floor are illustrative, not the real plan.",
        "source": SOURCE,
        "categories": {k: {"label": e, "label_hi": h} for k, (e, h) in CATEGORIES.items()},
        "blocks": [{"id": b, "name": e, "name_hi": h, "floors": fl} for b, e, h, fl in BUILDINGS],
        "floors": [{"id": f, "name": e, "name_hi": h} for f, e, h in FLOORS],
        "campus": CAMPUS,
        "units": units,
    }
    with open(os.path.join(HERE, "hospital.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    placed = sum(1 for u in units if "block" in u)
    print(f"wrote hospital.json: {placed} places with a location, {len(units) - placed} without "
          f"({sum(1 for u in units if u.get('unlisted'))} not in the booklet, {sum(1 for u in units if u.get('dmg'))} OPDs by cancer type), "
          f"{len(used)} building floors used")


if __name__ == "__main__":
    main()
