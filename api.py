# api.py
import io
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from datetime import datetime
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send"
]

# -----------------------------------------------------------------------------
# 1. CORE AUTHENTICATION & CONFIGURATION SETUP (VIA st.secrets)
# -----------------------------------------------------------------------------
# Safely pull simple string configuration IDs from the environment secrets config maps
sheet_id = st.secrets["GOOGLE_SHEET_ID"]
template_id = st.secrets["GOOGLE_TEMPLATE_ID"]
folder_id = st.secrets["GOOGLE_FOLDER_ID"]
YOUR_EMAIL = st.secrets["YOUR_EMAIL"] 

# Fetch the dictionary tree structure for the Google OAuth authorization block
creds_info = st.secrets["GOOGLE_SERVICE_ACCOUNT_CREDENTIALS"]
base_creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=SCOPES
)
creds = base_creds.with_subject(YOUR_EMAIL)

docs_service = build("docs", "v1", credentials=creds)
sheets_service = build("sheets", "v4", credentials=creds)
drive_service = build("drive", "v3", credentials=creds)
gmail_service = build("gmail", "v1", credentials=creds)

# -----------------------------------------------------------------------------
# 2. DATA PROCESSING AND METRICS PIPELINE (BATCH DRIVEN)
# -----------------------------------------------------------------------------

def get_participant_records():
    """
    Sweeps EVERY tab in the spreadsheet using efficient batch requests.
    Consolidates data into one network request to avoid hitting 429 Rate Limits.
    """
    try:
        spreadsheet_metadata = sheets_service.spreadsheets().get(
            spreadsheetId=sheet_id
        ).execute()
        
        all_tabs = spreadsheet_metadata.get("sheets", [])
        tab_titles = []

        for t in all_tabs:
            title = t.get("properties", {}).get("title", "").strip()
            if title and title not in (
                "Training List",
                "Index",
                "Splitting Names and Dates",
                "Overall Averages"
            ):
                tab_titles.append(title)
        
        if not tab_titles:
            return []

        ranges = [f"'{title}'!A1:Z" for title in tab_titles]
        batch_result = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=sheet_id,
            ranges=ranges
        ).execute()
        
        value_ranges = batch_result.get("valueRanges", [])
        master_records = []
        
        for value_range in value_ranges:
            range_title = value_range.get("range", "")
            title = range_title.split("!")[0].strip("'")
            
            rows = value_range.get("values", [])
            if not rows or len(rows) <= 1:
                continue
                
            headers = [h.strip() for h in rows[0] if h]
            status_idx = headers.index("Status") if "Status" in headers else len(headers)
            doc_id_idx = headers.index("Doc ID") if "Doc ID" in headers else status_idx + 1
            status_col_letter = chr(65 + status_idx)
            
            course_name = title 
            training_date = title.split(":", 1)[0] if ":" in title else "Unknown"
            
            for idx, r in enumerate(rows[1:]):
                if len(r) < 3 or not r[1].strip() or not r[2].strip(): 
                    continue
                
                master_records.append({
                    "row_num": idx + 2, 
                    "tab_name": title,
                    "name": f"{r[1].strip()} {r[2].strip()}",
                    "email": r[3].strip() if len(r) > 3 and r[3].strip() else "No Email",
                    "course": course_name,
                    "date": training_date,
                    "status": r[status_idx].strip() if len(r) > status_idx and r[status_idx].strip() else "Pending Send",
                    "doc_id": r[doc_id_idx].strip() if len(r) > doc_id_idx else None,
                    "status_col_letter": status_col_letter
                })
                
        return master_records
    except Exception as e:
        print(f"Error compiling master records list via batch: {e}")
        return []


def get_dashboard_metrics(sheets_service, spreadsheet_id):
    """
    Computes exact metrics in-memory from the batch dataset to ensure accuracy
    and instantly prevent API quota throttling.
    """
    try:
        current_month_prefix = f"{int(datetime.now().strftime('%m'))}." 
        
        records = get_participant_records()
        
        total_sent_all_time = 0  
        total_this_month_sent = 0
        total_pending_sends = 0
        
        for r in records:
            if r["status"] == "Sent":
                total_sent_all_time += 1
                
            if r["status"] == "Sent" and r["tab_name"].startswith(current_month_prefix):
                total_this_month_sent += 1
            elif r["status"] == "Pending Send":
                total_pending_sends += 1
                
        return {
            "total_all_time": total_sent_all_time,  
            "total_this_month": total_this_month_sent,
            "total_pending": total_pending_sends,
            "current_month_name": datetime.now().strftime("%B %Y")
        }
    except Exception as e:
        print(f"Error gathering metrics block: {e}")
        return {"total_all_time": 0, "total_this_month": 0, "total_pending": 0, "current_month_name": "Current Month"}


# -----------------------------------------------------------------------------
# 3. HELPER & CORE BACKEND EXECUTION UTILITIES
# -----------------------------------------------------------------------------

def get_or_create_tracking_columns(sheets_service, spreadsheet_id, tab_name):
    """
    Scans headers to safely locate index boundaries for writing.
    """
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!A1:Z1"
        ).execute()
        headers = result.get("values", [[]])[0]
        headers = [h.strip() for h in headers if h]
        
        if "Status" in headers and "Doc ID" in headers:
            return headers.index("Status"), headers.index("Doc ID")
        else:
            status_idx = len(headers)
            doc_id_idx = status_idx + 1
            status_letter, doc_letter = chr(65 + status_idx), chr(65 + doc_id_idx)
            
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!{status_letter}1:{doc_letter}1",
                valueInputOption="USER_ENTERED", body={"values": [["Status", "Doc ID"]]}
            ).execute()
            return status_idx, doc_id_idx
    except Exception:
        return 4, 5


def generate_certificates():
    """
    Finds missing certificates and updates dynamic columns.
    """
    master_range = "Training List!A:Z"
    result = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range=master_range).execute()
    rows = result.get("values", [])
    
    if not rows: return False
    valid_rows = [r for r in rows if len(r) >= 4 and r[2] and r[3]]
    if not valid_rows: return False

    last_row = valid_rows[-1]
    raw_date, raw_training_name = last_row[2].strip(), last_row[3].strip()
    trainer = last_row[4].strip() if len(last_row) >= 5 else ""
    ckc = last_row[5].strip() if len(last_row) >= 6 else ""
    set_level = last_row[6].strip() if len(last_row) >= 7 else ""
    hours = last_row[7].strip() if len(last_row) >= 8 else ""

    date_parts = raw_date.split(".")
    month_day = f"{date_parts[0]}.{date_parts[1]}" if len(date_parts) >= 2 else raw_date
    new_tab_name = f"{month_day}:{raw_training_name}"[:100]

    query = f"name = '{new_tab_name}' and '{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    folder_search = drive_service.files().list(q=query, spaces='drive', supportsAllDrives=True, includeItemsFromAllDrives=True, fields='files(id)').execute()
    existing_folders = folder_search.get('files', [])
    
    if existing_folders:
        target_subfolder_id = existing_folders[0]['id']
    else:
        new_folder = drive_service.files().create(
            body={'name': new_tab_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [folder_id]}, 
            supportsAllDrives=True, fields='id'
        ).execute()
        target_subfolder_id = new_folder.get('id')

    status_idx, doc_id_idx = get_or_create_tracking_columns(sheets_service, sheet_id, new_tab_name)
    status_letter, doc_letter = chr(65 + status_idx), chr(65 + doc_id_idx)

    try:
        result = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{new_tab_name}'!A2:Z").execute()
    except Exception:
        return False

    rows = result.get("values", [])
    for idx, r in enumerate(rows):
        if len(r) < 3 or not r[1].strip() or not r[2].strip(): continue
        current_row_number = idx + 2
        row_status = r[status_idx].strip() if len(r) > status_idx else ""
        
        if row_status: continue

        first_name, last_name = r[1].strip(), r[2].strip()
        full_name = f"{first_name} {last_name}"
        
        copy = drive_service.files().copy(
            fileId=template_id, supportsAllDrives=True,  
            body={"name": f"Temporary Doc - {full_name}", "parents": [target_subfolder_id]}
        ).execute()
        new_doc_id = copy["id"]

        requests = [
            {"replaceAllText": {"containsText": {"text": "{{First Name}}", "matchCase": True}, "replaceText": first_name}},
            {"replaceAllText": {"containsText": {"text": "{{Last Name}}", "matchCase": True}, "replaceText": last_name}},
            {"replaceAllText": {"containsText": {"text": "{{Name of Training}}", "matchCase": True}, "replaceText": raw_training_name}},
            {"replaceAllText": {"containsText": {"text": "{{Trainer}}", "matchCase": True}, "replaceText": trainer}},
            {"replaceAllText": {"containsText": {"text": "{{CKC}}", "matchCase": True}, "replaceText": ckc}},
            {"replaceAllText": {"containsText": {"text": "{{Set}}", "matchCase": True}, "replaceText": set_level}},
            {"replaceAllText": {"containsText": {"text": "{{Hours}}", "matchCase": True}, "replaceText": hours}},
            {"replaceAllText": {"containsText": {"text": "{{Date}}", "matchCase": True}, "replaceText": raw_date}}
        ]
        docs_service.documents().batchUpdate(documentId=new_doc_id, body={"requests": requests}).execute()
        pdf_data = drive_service.files().export(fileId=new_doc_id, mimeType="application/pdf").execute()

        uploaded_pdf_id = None
        try:
            media = MediaIoBaseUpload(io.BytesIO(pdf_data), mimetype="application/pdf")
            uploaded_file = drive_service.files().create(
                body={"name": f"{first_name}_{last_name}.pdf", "parents": [target_subfolder_id]},
                media_body=media, supportsAllDrives=True, fields="id"
            ).execute()
            uploaded_pdf_id = uploaded_file.get('id')
        except Exception:
            pass

        try:
            drive_service.files().delete(fileId=new_doc_id, supportsAllDrives=True).execute()
        except Exception:
            pass

        if uploaded_pdf_id:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id, range=f"'{new_tab_name}'!{status_letter}{current_row_number}:{doc_letter}{current_row_number}",
                valueInputOption="USER_ENTERED", body={"values": [["Pending Send", uploaded_pdf_id]]}
            ).execute()
    return True


def send_certificate_email(record):
    """
    Downloads and emails the PDF. Updates sheet status to 'Sent'.
    """
    if not record["doc_id"]: return False
    try:
        pdf_content = drive_service.files().get_media(fileId=record["doc_id"]).execute()
        message = MIMEMultipart()
        message["to"] = "giovanni@oregonask.org" 
        message["subject"] = f"Certificate of Completion: {record['course']}"
        
        body = f"Hello {record['name']},\n\nPlease find attached your certificate.\n\nBest regards,\nOregonASK"
        message.attach(MIMEText(body, "plain"))
        
        attachment = MIMEApplication(pdf_content, _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=f"{record['name'].replace(' ', '_')}.pdf")
        message.attach(attachment)
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        gmail_service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{record['tab_name']}'!{record['status_col_letter']}{record['row_num']}",
            valueInputOption="USER_ENTERED", body={"values": [["Sent"]]}
        ).execute()
        return True
    except Exception as e:
        print(f"❌ Actual Gmail/Drive Routing Error: {e}")
        return False