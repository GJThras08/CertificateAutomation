# generate_all.py
import io
import time
import sys
from googleapiclient.http import MediaIoBaseUpload
from api import (
    sheet_id, 
    template_id, 
    folder_id, 
    get_google_services, # Dynamically retrieve credentials context
    get_participant_records,
    get_or_create_tracking_columns
)

def run_one_time_bulk_generation(stream_mode=True):
    """
    Runs the certificate generation pipeline.
    If stream_mode=True (Streamlit), it yields text messages.
    If stream_mode=False (Cron/Task Scheduler), it prints directly to standard output logs.
    """
    def log(message):
        if stream_mode:
            return message
        else:
            print(message)
            sys.stdout.flush() 
            return None

    msg = log("🚀 Initializing deep system scan for ungenerated certificates...")
    if msg: yield msg
    
    # Instantiate client services dynamically for active user context
    services = get_google_services()
    sheets_service = services["sheets"]
    drive_service = services["drive"]
    docs_service = services["docs"]
    
    all_records = get_participant_records()
    
    if not all_records:
        msg = log("❌ No valid participant records or tabs found to process.")
        if msg: yield msg
        return

    missing_certs = [r for r in all_records if not r["doc_id"]]
    
    if not missing_certs:
        msg = log("✨ Status Green: 100% of rows already possess generated certificates in Google Drive!")
        if msg: yield msg
        return

    msg = log(f"📦 Found {len(missing_certs)} certificates needing generation across your workbook sheets.")
    if msg: yield msg
    
    folder_cache = {}
    success_count = 0

    for idx, record in enumerate(missing_certs):
        tab_name = record["tab_name"]
        full_name = record["name"]
        
        msg = log(f"🔄 [{idx + 1}/{len(missing_certs)}] Processing: {full_name} for '{tab_name}'...")
        if msg: yield msg
        
        if tab_name not in folder_cache:
            query = f"name = '{tab_name}' and '{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            folder_search = drive_service.files().list(
                q=query, spaces='drive', supportsAllDrives=True, includeItemsFromAllDrives=True, fields='files(id)'
            ).execute()
            existing_folders = folder_search.get('files', [])
            
            if existing_folders:
                target_subfolder_id = existing_folders[0]['id']
            else:
                msg = log(f"📁 Creating new directory for training: '{tab_name}'")
                if msg: yield msg
                new_folder = drive_service.files().create(
                    body={'name': tab_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [folder_id]}, 
                    supportsAllDrives=True, fields='id'
                ).execute()
                target_subfolder_id = new_folder.get('id')
            
            folder_cache[tab_name] = target_subfolder_id
        else:
            target_subfolder_id = folder_cache[tab_name]

        status_idx, doc_id_idx = get_or_create_tracking_columns(sheets_service, sheet_id, tab_name)
        status_letter = record["status_col_letter"]
        doc_letter = chr(65 + doc_id_idx)

        if ":" in tab_name:
            training_date, raw_training_name = tab_name.split(":", 1)
        else:
            training_date, raw_training_name = "Unknown", tab_name

        name_parts = full_name.split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        try:
            copy = drive_service.files().copy(
                fileId=template_id, supportsAllDrives=True,  
                body={"name": f"Temporary Doc - {full_name}", "parents": [target_subfolder_id]}
            ).execute()
            new_doc_id = copy["id"]

            requests = [
                {"replaceAllText": {"containsText": {"text": "{{First Name}}", "matchCase": True}, "replaceText": first_name}},
                {"replaceAllText": {"containsText": {"text": "{{Last Name}}", "matchCase": True}, "replaceText": last_name}},
                {"replaceAllText": {"containsText": {"text": "{{Name of Training}}", "matchCase": True}, "replaceText": raw_training_name}},
                {"replaceAllText": {"containsText": {"text": "{{Trainer}}", "matchCase": True}, "replaceText": "OregonASK Team"}},
                {"replaceAllText": {"containsText": {"text": "{{CKC}}", "matchCase": True}, "replaceText": "General"}},
                {"replaceAllText": {"containsText": {"text": "{{Set}}", "matchCase": True}, "replaceText": "1"}},
                {"replaceAllText": {"containsText": {"text": "{{Hours}}", "matchCase": True}, "replaceText": "2"}},
                {"replaceAllText": {"containsText": {"text": "{{Date}}", "matchCase": True}, "replaceText": training_date}}
            ]
            docs_service.documents().batchUpdate(documentId=new_doc_id, body={"requests": requests}).execute()
            
            pdf_data = drive_service.files().export(fileId=new_doc_id, mimeType="application/pdf").execute()

            media = MediaIoBaseUpload(io.BytesIO(pdf_data), mimetype="application/pdf")
            uploaded_file = drive_service.files().create(
                body={"name": f"{first_name}_{last_name}.pdf", "parents": [target_subfolder_id]},
                media_body=media, supportsAllDrives=True, fields="id"
            ).execute()
            uploaded_pdf_id = uploaded_file.get('id')

            drive_service.files().delete(fileId=new_doc_id, supportsAllDrives=True).execute()

            if uploaded_pdf_id:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=sheet_id, 
                    range=f"'{tab_name}'!{status_letter}{record['row_num']}:{doc_letter}{record['row_num']}",
                    valueInputOption="USER_ENTERED", 
                    body={"values": [["Pending Send", uploaded_pdf_id]]}
                ).execute()
                success_count += 1
                
            time.sleep(0.5)

        except Exception as row_error:
            msg = log(f"⚠️ Failed to compile row details for {full_name}: {row_error}")
            if msg: yield msg
            continue

    msg = log(f"✅ Generation pipeline finished successfully! Created {success_count} missing certificates.")
    if msg: yield msg

if __name__ == "__main__":
    list(run_one_time_bulk_generation(stream_mode=False))