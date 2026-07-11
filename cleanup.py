# cleanup.py
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# 1. Reuse your exact credential pathway
base_creds = service_account.Credentials.from_service_account_file(
    "certificate-automation-498912-4c5843774323.json", scopes=SCOPES
)
YOUR_EMAIL = "giovanni@oregonask.org" 
creds = base_creds.with_subject(YOUR_EMAIL)
sheets_service = build("sheets", "v4", credentials=creds)

sheet_id = "1oCOciE-qdt339uyKdfEtX1JG88WU1kgQQAl5KCpIS9o"


def run_one_time_status_sync():
    print("🚀 Initializing one-time historical certificate sync...")
    try:
        # 1. Get all tabs in the workbook
        spreadsheet_metadata = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        all_tabs = spreadsheet_metadata.get("sheets", [])
        tab_titles = [
            t.get("properties", {}).get("title", "").strip() 
            for t in all_tabs 
            if t.get("properties", {}).get("title", "").strip() and t.get("properties", {}).get("title", "").strip() != "Training List"
        ]
        
        if not tab_titles:
            print("❌ No valid training tabs found to process.")
            return

        # 2. Batch fetch all tab data at once to prevent 429 quota limits
        ranges = [f"'{title}'!A1:Z" for title in tab_titles]
        batch_result = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=sheet_id, ranges=ranges
        ).execute()
        
        value_ranges = batch_result.get("valueRanges", [])
        
        # 3. Prepare a collection to hold all updates so we can submit them together
        batch_update_data = []
        rows_to_update_count = 0

        print(f"📦 Analyzing {len(value_ranges)} tabs for empty statuses...")

        for value_range in value_ranges:
            range_title = value_range.get("range", "")
            title = range_title.split("!")[0].strip("'")
            rows = value_range.get("values", [])
            
            if not rows or len(rows) <= 1:
                continue
                
            # Locate where 'Status' column lives dynamically for this tab
            headers = [h.strip() for h in rows[0] if h]
            if "Status" not in headers:
                print(f"⚠️ Skipping tab '{title}': No 'Status' header found.")
                continue
                
            status_idx = headers.index("Status")
            status_col_letter = chr(65 + status_idx)
            
            # Look at participant data rows
            for idx, r in enumerate(rows[1:]):
                row_num = idx + 2 # Account for header row exclusion
                
                # Verify row has participant names
                if len(r) < 3 or not r[1].strip() or not r[2].strip(): 
                    continue
                
                # Check current cell value at the Status column index
                current_status = r[status_idx].strip() if len(r) > status_idx else ""
                
                # CRITICAL: Only target completely empty status fields
                if not current_status:
                    batch_update_data.append({
                        "range": f"'{title}'!{status_col_letter}{row_num}",
                        "values": [["Sent"]]
                    })
                    rows_to_update_count += 1

        # 4. If any empty statuses are caught, fire a single write request back to Google
        if batch_update_data:
            print(f"⚡ Writing 'Sent' back to {rows_to_update_count} rows across your sheets via batch payload...")
            
            body = {
                "valueInputOption": "USER_ENTERED",
                "data": batch_update_data
            }
            
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body=body
            ).execute()
            
            print("✅ Status sync complete! All past rows successfully marked as 'Sent'.")
        else:
            print("✨ Perfect status synchronization! Zero empty cells found across historical logs.")

    except Exception as e:
        print(f"❌ Critical Error executing cleanup: {e}")


if __name__ == "__main__":
    run_one_time_status_sync()