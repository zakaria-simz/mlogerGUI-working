import re
import os
import json
import shutil
import socket
import concurrent.futures
import requests
import io
from datetime import datetime as dt, timedelta, date
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

# Google Drive Imports
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    DRIVE_AVAILABLE = True
except ImportError:
    DRIVE_AVAILABLE = False
    print("Warning: Google Drive libraries not found. Install with: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")

TIME_FORMAT = "%d/%m/%Y"
SETTINGS_FILE = "settings.json"
BACKUP_DIR = "backups"
SCOPES = ['https://www.googleapis.com/auth/drive.file']

@dataclass
class Transaction:
    amount: float
    tags: List[str]

    def to_string(self) -> str:
        base = f"{self.amount:g}"
        if self.tags:
            return f"{base}({', '.join(self.tags)})"
        return base

@dataclass
class DailyRecord:
    date: date
    balance_snapshot: Optional[float] = None
    transactions: List[Transaction] = field(default_factory=list)

    @property
    def total_change(self) -> float:
        return sum(t.amount for t in self.transactions)

    @property
    def income(self) -> float:
        return sum(t.amount for t in self.transactions if t.amount > 0)

    @property
    def expense(self) -> float:
        return sum(t.amount for t in self.transactions if t.amount < 0)

    def to_dict(self):
        return {
            "date": self.date.strftime(TIME_FORMAT),
            "day_name": self.date.strftime("%a"),
            "net_change": self.total_change,
            "income": self.income,
            "expense": self.expense,
            "balance_snapshot": self.balance_snapshot,
            "transactions": [
                {"amount": t.amount, "tags": t.tags} for t in self.transactions
            ]
        }
    
    def to_file_line(self) -> str:
        day_str = self.date.strftime("%a")
        date_str = self.date.strftime(TIME_FORMAT)
        bal_str = f"{self.balance_snapshot:g}" if self.balance_snapshot is not None else ""
        trans_parts = [t.to_string() for t in self.transactions]
        trans_str = " ".join(trans_parts)
        return f"{day_str} {date_str}: {bal_str.center(5)} :{trans_str}"

class QueryParser:
    def __init__(self):
        self.operators = {
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
        }

    def parse_value(self, val_str: str):
        val_str = str(val_str).strip()
        if val_str == "today": return date.today()
        if val_str == "yesterday": return date.today() - timedelta(days=1)
        try: return float(val_str)
        except ValueError: pass
        try: return dt.strptime(val_str, TIME_FORMAT).date()
        except ValueError: pass
        return val_str

    def evaluate(self, record: DailyRecord, query: str) -> bool:
        if not query or query.strip() == "": return True
        conditions = query.split('&')
        results = []
        for cond in conditions:
            cond = cond.strip()
            match = re.search(r"(date|net|income|expense|tag|day)\s*(==|!=|>=|<=|>|<)\s*(.+)", cond, re.IGNORECASE)
            if not match: continue
            field_name, op, val_str = match.groups()
            target_val = self.parse_value(val_str)
            operator_func = self.operators.get(op)
            
            if field_name == "date": res = operator_func(record.date, target_val)
            elif field_name == "net": res = operator_func(record.total_change, target_val)
            elif field_name == "income": res = operator_func(record.income, target_val)
            elif field_name == "expense": res = operator_func(abs(record.expense), target_val)
            elif field_name == "day": res = operator_func(record.date.strftime("%a").lower(), str(target_val).lower())
            elif field_name == "tag":
                all_tags = set()
                for t in record.transactions: all_tags.update(t.tags)
                has_tag = str(target_val) in all_tags
                res = has_tag if op == "==" else (not has_tag if op == "!=" else False)
            else: res = False
            results.append(res)
        return all(results)

class DriveManager:
    def __init__(self, creds_path, token_path):
        self.creds_path = creds_path
        self.token_path = token_path
        self.service = None
        self.folder_name = "mLoger_Backups"
    
    def authenticate(self):
        if not DRIVE_AVAILABLE: raise Exception("Google Drive libs missing")
        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.creds_path):
                    raise FileNotFoundError(f"Credentials file not found at {self.creds_path}")
                flow = InstalledAppFlow.from_client_secrets_file(self.creds_path, SCOPES)
                creds = flow.run_local_server(port=0)
            
            with open(self.token_path, 'w') as token:
                token.write(creds.to_json())
        
        self.service = build('drive', 'v3', credentials=creds)
        return True

    def _get_folder_id(self):
        # Find folder
        results = self.service.files().list(
            q=f"mimeType='application/vnd.google-apps.folder' and name='{self.folder_name}' and trashed=false",
            fields="files(id, name)").execute()
        items = results.get('files', [])
        
        if not items:
            # Create folder
            file_metadata = {'name': self.folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
            file = self.service.files().create(body=file_metadata, fields='id').execute()
            return file.get('id')
        else:
            return items[0]['id']

    def upload_file(self, filepath):
        if not self.service: self.authenticate()
        folder_id = self._get_folder_id()
        name = f"cache_{dt.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        
        file_metadata = {'name': name, 'parents': [folder_id]}
        media = MediaFileUpload(filepath, mimetype='text/plain')
        
        file = self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return file.get('id'), name

    def list_versions(self):
        if not self.service: self.authenticate()
        folder_id = self._get_folder_id()
        results = self.service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            orderBy="createdTime desc",
            fields="files(id, name, createdTime, size)").execute()
        return results.get('files', [])

    def download_content(self, file_id):
        if not self.service: self.authenticate()
        request = self.service.files().get_media(fileId=file_id)
        file_io = io.BytesIO()
        downloader = MediaIoBaseDownload(file_io, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        return file_io.getvalue().decode('utf-8')

class ExpenseManager:
    def __init__(self, filepath="cache.txt"):
        self.filepath = filepath
        self.records: Dict[date, DailyRecord] = {}
        self.query_engine = QueryParser()
        self.settings = self._load_settings()
        
        if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
        if os.path.exists(filepath): self.reload_from_file()
        
        # Init Drive Manager if settings exist
        self.drive_mgr = None
        if self.settings.get('drive_enabled'):
            self._init_drive()

        if self.settings.get("auto_fetch", False):
            pass # Auto fetch routine

    def _init_drive(self):
        creds = self.settings.get('drive_creds_path', 'credentials.json')
        token = self.settings.get('drive_token_path', 'token.json')
        self.drive_mgr = DriveManager(creds, token)

    # --- Drive API Exposed to UI ---
    def get_drive_settings(self):
        return {"enabled":self.settings['drive_enabled']}

    def save_drive_settings(self, enabled, creds_path, token_path):
        self.settings['drive_enabled'] = enabled
        self.settings['drive_creds_path'] = creds_path
        self.settings['drive_token_path'] = token_path
        self._save_settings()
        
        if enabled:
            try:
                self._init_drive()
                # Run auth immediately to verify/generate token
                self.drive_mgr.authenticate()
                return {"status": "success", "message": "Drive authenticated successfully"}
            except Exception as e:
                self.settings['drive_enabled'] = False # Revert on failure
                self._save_settings()
                return {"status": "error", "message": str(e)}
        return {"status": "success", "message": "Settings saved"}

    def drive_push(self):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            self.save_to_file() # Ensure latest is saved
            fid, name = self.drive_mgr.upload_file(self.filepath)
            
            self.settings['last_synced'] = dt.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_settings()
            
            return {"status": "success", "message": f"Uploaded {name}", "last_synced": self.settings['last_synced']}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def drive_list_versions(self):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            files = self.drive_mgr.list_versions()
            return {"status": "success", "files": files}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def drive_preview_version(self, file_id):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            content = self.drive_mgr.download_content(file_id)
            # Simple stats for preview
            lines = content.splitlines()
            count = 0
            for l in lines:
                if self._parse_line(l): count += 1
            
            preview_text = f"Records: {count}\nFirst 5 lines:\n" + "\n".join(lines[:5])
            return {"status": "success", "content": content, "preview": preview_text}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def drive_apply_version(self, file_id):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            # backup current first
            self.create_backup("predrive_restore")
            
            content = self.drive_mgr.download_content(file_id)
            with open(self.filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            
            self.reload_from_file()
            self.settings['last_synced'] = dt.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_settings()
            
            return {"status": "success", "message": "Version applied successfully"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # --- Rest of existing ExpenseManager methods (abbreviated) ---
    def scan_network_and_sync(self):
        try:
            discovered_url = self.get_source_url()
            messages = []
            if discovered_url:
                messages.append(f"Found Server: {discovered_url}")
                if discovered_url not in self.settings["sources"]:
                    self.add_source(discovered_url)
                    messages.append("Added to sources list.")
            else:
                messages.append("No local server found on port 8181.")
            sync_results = self.sync_all_sources()
            messages.extend(sync_results)
            return {"status": "success", "message": "\n".join(messages)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            IP = s.getsockname()[0]
        except Exception: IP = '127.0.0.1'
        finally: s.close()
        return IP

    def _check_server(self, ip):
        url = f"http://{ip}:8181/data/cache.txt"
        try:
            requests.head(url, timeout=0.2)
            return url
        except: return None

    def get_source_url(self):
        local_ip = self.get_local_ip()
        if local_ip == '127.0.0.1': return None
        base_ip = ".".join(local_ip.split(".")[:-1])
        ips = [f"{base_ip}.{i}" for i in range(1, 255)]
        found_url = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            results = executor.map(self._check_server, ips)
            for url in results:
                if url:
                    found_url = url
                    break 
        return found_url

    def _load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try: 
                with open(SETTINGS_FILE, 'r') as f: return json.load(f)
            except: pass
        return {"sources": [], "auto_fetch": True, "drive_enabled": False}

    def _save_settings(self):
        with open(SETTINGS_FILE, 'w') as f: json.dump(self.settings, f, indent=4)

    def add_source(self, url: str):
        if url not in self.settings["sources"]:
            self.settings["sources"].append(url)
            self._save_settings()
        return self.settings["sources"]

    def remove_source(self, url: str):
        if url in self.settings["sources"]:
            self.settings["sources"].remove(url)
            self._save_settings()
        return self.settings["sources"]

    def get_sources(self): return self.settings["sources"]

    def sync_all_sources(self):
        results = []
        for src in self.settings["sources"]:
            res = self.fetch_and_merge(src)
            status = "Updated" if res.get("updated", 0) > 0 else "No new data"
            if res.get("status") == "error": status = f"Error: {res.get('message')}"
            results.append(f"{src}: {status}")
        return results

    def create_backup(self, reason="manual"):
        if not os.path.exists(self.filepath): return
        timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"cache_{timestamp}_{reason}.txt"
        shutil.copy(self.filepath, os.path.join(BACKUP_DIR, backup_name))
        return backup_name

    def get_backups(self):
        if not os.path.exists(BACKUP_DIR): return []
        files = sorted(os.listdir(BACKUP_DIR), reverse=True)
        return files[:10]

    def restore_backup(self, backup_filename):
        src = os.path.join(BACKUP_DIR, backup_filename)
        if os.path.exists(src):
            self.create_backup("prerestore")
            shutil.copy(src, self.filepath)
            self.reload_from_file()
            return {"status": "success", "message": f"Restored {backup_filename}"}
        return {"status": "error", "message": "Backup file not found"}

    def _parse_line(self, line: str) -> Optional[DailyRecord]:
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"): return None
        if "#" in line: line = line.split("#")[0]
        try:
            parts = line.split(":")
            if len(parts) < 3: return None
            date_part = parts[0]
            balance_part = parts[1]
            trans_part = ":".join(parts[2:])
            d_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_part)
            if not d_match: return None
            rec_date = dt.strptime(d_match.group(1), TIME_FORMAT).date()
            balance = None
            if balance_part.strip():
                try: balance = float(balance_part.strip())
                except: pass
            transaction_pattern = r"(?P<amt>[+\-]?\s*\d+(?:\.\d+)?)\s*(?:\((?P<tags>[^)]*)\))?"
            transactions = []
            for match in re.finditer(transaction_pattern, trans_part):
                amount_str = match.group("amt")
                tags_str = match.group("tags")
                amount_val = float(amount_str.replace(" ", ""))
                tag_list = []
                if tags_str: tag_list = [t.strip() for t in tags_str.split(",") if t.strip()]
                transactions.append(Transaction(amount_val, tag_list))
            return DailyRecord(rec_date, balance, transactions)
        except Exception: return None

    def reload_from_file(self):
        self.records = {}
        if not os.path.exists(self.filepath): return
        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                rec = self._parse_line(line)
                if rec: self.records[rec.date] = rec
        return {"status": "success"}

    def save_to_file(self, backup=True):
        if backup: self.create_backup("autosave")
        sorted_records = sorted(self.records.values(), key=lambda r: r.date)
        with open(self.filepath, "w", encoding="utf-8") as f:
            for rec in sorted_records:
                f.write(rec.to_file_line() + "\n")
        return {"status": "success"}

    def update_transaction(self, date_str: str, trans_index: int, amount: float, tags: List[str]):
        try:
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date not in self.records: return {"status": "error", "message": "Date not found"}
            record = self.records[target_date]
            if trans_index < 0 or trans_index >= len(record.transactions): return {"status": "error", "message": "Idx out of bounds"}
            record.transactions[trans_index] = Transaction(amount, tags)
            self.save_to_file()
            return {"status": "success"}
        except Exception as e: return {"status": "error", "message": str(e)}

    def add_transaction(self, date_str: str, amount: float, tags: List[str]):
        try:
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date not in self.records: self.records[target_date] = DailyRecord(target_date, None, [])
            self.records[target_date].transactions.append(Transaction(amount, tags))
            self.save_to_file()
            return {"status": "success"}
        except Exception as e: return {"status": "error", "message": str(e)}
            
    def delete_transaction(self, date_str: str, trans_index: int):
        try:
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date in self.records:
                if 0 <= trans_index < len(self.records[target_date].transactions):
                    self.records[target_date].transactions.pop(trans_index)
                    self.save_to_file()
                    return {"status": "success"}
            return {"status": "error", "message": "Record not found"}
        except Exception as e: return {"status": "error", "message": str(e)}

    def fetch_and_merge(self, source: str):
        lines = []
        if source.startswith("http"):
            try: lines = requests.get(source, timeout=2).text.splitlines()
            except Exception as e: return {"status": "error", "message": str(e)}
        elif os.path.exists(source):
            with open(source, "r", encoding="utf-8") as f: lines = f.readlines()
        else: return {"status": "error", "message": "Source not found"}
        updated = 0
        for line in lines:
            rec = self._parse_line(line)
            if rec:
                self.records[rec.date] = rec
                updated += 1
        self.save_to_file()
        return {"status": "success", "updated": updated, "message": f"Merged {updated} records"}

    def replace_database_file(self, source: str):
        self.create_backup("beforereplace")
        self.records = {}
        return self.fetch_and_merge(source)

    def get_records(self, filter_query: str = "") -> List[Dict]:
        result = []
        sorted_recs = sorted(self.records.values(), key=lambda r: r.date, reverse=True)
        for rec in sorted_recs:
            if self.query_engine.evaluate(rec, filter_query):
                result.append(rec.to_dict())
        return result

    def get_statistics(self, filter_query: str = "") -> Dict[str, Any]:
        filtered = [r for r in self.records.values() if self.query_engine.evaluate(r, filter_query)]
        if not filtered: return {"count": 0, "net_savings": 0, "total_income": 0, "total_expense": 0, "tag_breakdown": {}}
        total_income = sum(r.income for r in filtered)
        total_expense = sum(r.expense for r in filtered)
        tag_spending = defaultdict(float)
        for rec in filtered:
            for t in rec.transactions:
                if t.amount < 0:
                    for tag in t.tags: tag_spending[tag] += abs(t.amount)
        return {
            "record_count": len(filtered),
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net_savings": round(total_income + total_expense, 2),
            "tag_breakdown": dict(sorted(tag_spending.items(), key=lambda x: x[1], reverse=True)[:5])
        }

    def get_current_balance(self):
        return round(sum(r.total_change for r in self.records.values()), 2)